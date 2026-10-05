"""Real Gazebo/PX4 observers used by the opt-in flight acceptance suite."""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
from websockets.sync.client import connect

from app.gazebo.world import World, _load_bindings


BACKEND = os.getenv("BACKEND_URL", "http://backend:8003").rstrip("/")
HUB = os.getenv("HUB_URL", "http://px4-hub:8002").rstrip("/")
ARTIFACTS = Path(os.getenv("FLIGHT_ARTIFACTS", "/artifacts"))
READY_TIMEOUT = 120
MANEUVER_TIMEOUT = 30
MISSION_TIMEOUT = 180
RETURN_TIMEOUT = 180


class TelemetryObserver:
    """Independent public realtime observer; it outlives a control WebSocket."""

    def __init__(self, drone_id: str):
        self.drone_id = drone_id
        self.samples: queue.Queue = queue.Queue()
        self.latest_by_type: dict[str, dict] = {}
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error: str | None = None
        self.records: list[dict] = []
        self.last_record_by_type: dict[str, float] = {}

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        url = BACKEND.replace("http://", "ws://").replace("https://", "wss://") + "/api/v1/realtime"
        try:
            with connect(url, open_timeout=10, close_timeout=2, max_size=1_000_000) as ws:
                ws.send(json.dumps({"action": "subscribe", "channels": [
                    f"drone.{self.drone_id}.telemetry", f"drone.{self.drone_id}.flight"]}))
                ws.socket.settimeout(.5)
                while not self.stop_event.is_set():
                    try:
                        raw = ws.recv()
                    except TimeoutError:
                        continue
                    event = json.loads(raw)
                    if event.get("type") in {"error", "invalidated"}:
                        raise RuntimeError(f"Realtime observer {event}")
                    if event.get("type") == "subscribed":
                        continue
                    if not (event.get("channel", "").endswith(".telemetry") or
                            event.get("channel", "").endswith(".flight")):
                        continue
                    entry = {"monotonic": time.monotonic(), **event}
                    with self.condition:
                        raw_type = event.get("data", {}).get("type")
                        if raw_type:
                            self.latest_by_type[raw_type] = entry
                        now = entry["monotonic"]
                        if raw_type != "attitude" or now - self.last_record_by_type.get(raw_type, 0) >= .1:
                            self.records.append(entry)
                            self.last_record_by_type[raw_type] = now
                        self.samples.put(entry)
                        self.condition.notify_all()
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = repr(exc)

    def latest(self, channel_suffix: str, timeout: float = 3):
        deadline = time.monotonic() + timeout
        retained = []
        while time.monotonic() < deadline:
            if self.error:
                raise AssertionError(f"Telemetry observer failed: {self.error}")
            try:
                event = self.samples.get(timeout=max(.01, min(.25, deadline - time.monotonic())))
            except queue.Empty:
                continue
            retained.append(event)
            if event.get("channel", "").endswith(channel_suffix):
                for item in retained:
                    self.samples.put(item)
                return event
        for item in retained:
            self.samples.put(item)
        raise AssertionError(f"No fresh realtime event for {channel_suffix}")

    def telemetry(self, required=("position", "velocity", "armed", "landed_state"), timeout=3):
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                if self.error:
                    raise AssertionError(f"Telemetry observer failed: {self.error}")
                snapshot = {key: value for key, value in self.latest_by_type.items()
                            if time.monotonic() - value["monotonic"] <= 5}
                if all(key in snapshot for key in required):
                    return {key: value["data"].get("data") for key, value in snapshot.items()}
                self.condition.wait(min(.2, max(.01, deadline - time.monotonic())))
        raise AssertionError(f"Fresh PX4 telemetry types unavailable: {required}; got {list(snapshot)}")

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=3)


class HubTelemetryObserver:
    """PX4 telemetry stream through Hub, independent of Backend availability."""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self.latest_by_type: dict[str, dict] = {}
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        url = HUB.replace("http://", "ws://").replace("https://", "wss://")
        url += f"/api/v1/instances/{self.instance_id}/telemetry"
        try:
            with connect(url, open_timeout=10, close_timeout=2, max_size=1_000_000) as ws:
                ws.socket.settimeout(.5)
                while not self.stop_event.is_set():
                    try:
                        event = json.loads(ws.recv())
                    except TimeoutError:
                        continue
                    with self.condition:
                        self.latest_by_type[event["type"]] = {
                            "monotonic": time.monotonic(), "data": event.get("data")}
                        self.condition.notify_all()
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = repr(exc)

    def telemetry(self, required=("position", "velocity", "armed", "landed_state"), timeout=3):
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                if self.error:
                    raise AssertionError(f"Hub telemetry observer failed: {self.error}")
                now = time.monotonic()
                fresh = {key: value for key, value in self.latest_by_type.items()
                         if now - value["monotonic"] <= 5}
                if all(key in fresh for key in required):
                    return {key: value["data"] for key, value in fresh.items()}
                self.condition.wait(min(.2, max(.01, deadline - time.monotonic())))
        raise AssertionError(f"Fresh Hub telemetry unavailable for {required}: {list(fresh)}")

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=3)


class Px4LogObserver:
    """Collect PX4 process output before its instance directory is deleted."""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self.records: list[dict] = []
        self.last_message = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        url = HUB.replace("http://", "ws://").replace("https://", "wss://")
        url += f"/api/v1/instances/{self.instance_id}/logs"
        try:
            with connect(url, open_timeout=10, close_timeout=1, max_size=1_000_000) as ws:
                ws.socket.settimeout(.5)
                while not self.stop_event.is_set():
                    try:
                        event = json.loads(ws.recv())
                    except TimeoutError:
                        continue
                    record = {"monotonic": time.monotonic(), **event}
                    message = record.get("message", "")
                    message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message).replace("\r", "\n")
                    message = re.sub(r"(?:pxh>\s*)+", "", message).strip()
                    if (not re.search(r"(?:INFO|WARN|ERROR|CRITICAL|DEBUG)\s+\[[^]]+\]", message)
                            or message == self.last_message):
                        continue
                    record["message"] = message[:8192]
                    self.last_message = message
                    # PX4 emits a repeated shell prompt continuously; cap the
                    # diagnostic stream so it cannot dominate flight evidence.
                    if len(self.records) >= 20_000:
                        self.records.pop(0)
                    self.records.append(FlightEnvironment._redact(record))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.records.append({"monotonic": time.monotonic(), "observer_error": repr(exc)})

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


class GazeboPoseObserver:
    """Direct Gazebo Transport observer, independent of Backend pose cache."""

    def __init__(self):
        self.world = World("empty", timeout_ms=3000)
        bindings = _load_bindings()
        self.pose_type = bindings["Pose_V"]
        self.node = self.world.node
        self.condition = threading.Condition()
        self.poses: dict[str, dict] = {}
        self.sim_time = None
        self.received = 0
        self.records: list[dict] = []
        self.tracked_names: set[str] = set()
        self.last_record_at = 0.0
        self.topic = "/world/empty/pose/info"
        self.stats_topic = "/world/empty/stats"
        if not self.node.subscribe(self.pose_type, self.topic, self._on_pose):
            raise RuntimeError(f"Gazebo Transport subscription failed: {self.topic}")
        if not self.node.subscribe(_load_bindings()["WorldStatistics"], self.stats_topic, self._on_stats):
            self.node.unsubscribe(self.topic)
            raise RuntimeError(f"Gazebo Transport subscription failed: {self.stats_topic}")

    def _on_stats(self, message):
        with self.condition:
            self.sim_time = message.sim_time.sec + message.sim_time.nsec / 1_000_000_000
            self.condition.notify_all()

    def _on_pose(self, message):
        now = time.monotonic()
        with self.condition:
            for pose in message.pose:
                name = pose.name.split("::", 1)[0]
                self.poses[name] = {"monotonic": now, "sim_time_s": self.sim_time,
                    "position": {"x": pose.position.x, "y": pose.position.y, "z": pose.position.z},
                    "orientation": {"x": pose.orientation.x, "y": pose.orientation.y,
                                    "z": pose.orientation.z, "w": pose.orientation.w}}
                if name in self.tracked_names and now - self.last_record_at >= .1:
                    self.records.append({"name": name, **self.poses[name]})
                    self.last_record_at = now
            self.received += 1
            self.condition.notify_all()

    def current(self, model_name: str, timeout: float = 3):
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                if model_name in self.poses:
                    return self.poses[model_name].copy()
                self.condition.wait(max(0, deadline - time.monotonic()))
        raise AssertionError(f"No runtime pose from Gazebo for {model_name}")

    def close(self):
        self.node.unsubscribe(self.topic)
        self.node.unsubscribe(self.stats_topic)


class GazeboNavSatObserver:
    """Direct raw Gazebo NavSat samples for reset/startup diagnostics."""

    def __init__(self, topic):
        self.world = World("empty", timeout_ms=3000)
        self.node = self.world.node
        self.samples = []
        self.lock = threading.Lock()
        self.topic = topic
        message_type = _load_bindings()["sensor_types"]["gz.msgs.NavSat"]
        if not self.node.subscribe(message_type, topic, self._on_sample):
            raise RuntimeError(f"Gazebo NavSat subscription failed: {topic}")

    def _on_sample(self, message):
        sample = {"monotonic": time.monotonic(), **{
            name: getattr(message, name) for name in
            ("latitude_deg", "longitude_deg", "altitude", "velocity_east",
             "velocity_north", "velocity_up")}}
        with self.lock:
            self.samples.append(sample)
            if len(self.samples) > 2000:
                del self.samples[:1000]

    def snapshot(self):
        with self.lock:
            return list(self.samples)

    def close(self):
        self.node.unsubscribe(self.topic)


class FlightEnvironment:
    def __init__(self):
        self.http = httpx.Client(timeout=15)
        self.gazebo = GazeboPoseObserver()
        self.navsat_observers: list[GazeboNavSatObserver] = []
        self.observers: list[TelemetryObserver] = []
        self.hub_observers: list[HubTelemetryObserver] = []
        self.px4_logs: dict[str, Px4LogObserver] = {}
        self.instance_ids: dict[str, str] = {}
        self.archived_instances: set[str] = set()
        self.drones: list[str] = []
        self.stations: dict[str, dict] = {}
        self.geo_context: dict = {}
        self.command_log: list[dict] = []

    def request(self, method: str, path: str, *, expected=(200,), **kwargs):
        started = time.monotonic()
        url = path if path.startswith(("http://", "https://")) else BACKEND + path
        try:
            response = self.http.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
        except httpx.HTTPError as exc:
            self.command_log.append({"monotonic": started, "method": method, "path": path,
                                     "status": None, "error": repr(exc)})
            raise
        self.command_log.append({"monotonic": started, "method": method, "path": path,
                                 "status": response.status_code,
                                 "response": self._redact(response.json()) if response.content else None})
        if response.status_code not in expected:
            raise AssertionError(f"{method} {path}: {response.status_code} {response.text}")
        return response

    @staticmethod
    def _redact(value):
        if isinstance(value, dict):
            return {key: FlightEnvironment._redact(item) for key, item in value.items()
                    if key.lower() not in {"token", "authorization", "password"}}
        if isinstance(value, list):
            return [FlightEnvironment._redact(item) for item in value]
        return value

    def create_drone(self, pad_id=None, timeout=30):
        self.geo_context = self.request("GET", "/api/v1/world").json()["spherical_coordinates"]
        if pad_id is None:
            pads = self.request("GET", "/api/v1/world/spawn-pads").json()["pads"]
            pad_id = next(pad["id"] for pad in pads if pad["availability"] == "available")
        response = self.request("POST", "/api/v1/drones/", expected=(201,), timeout=timeout, json={
            "model": "x500_gimbal", "name": f"flight_{int(time.time())}_{len(self.drones)}",
            "spawn_pad_id": pad_id,
        }).json()
        assert response["status"] == "ready"
        drone_id = response["id"]
        self.drones.append(drone_id)
        self.stations[drone_id] = response["simulation"]["pose"]
        self.gazebo.tracked_names.add(response["name"])
        autopilot = self.request("GET", f"/api/v1/drones/{drone_id}/autopilot").json()
        instance_id = autopilot.get("id")
        if instance_id:
            self.instance_ids[drone_id] = instance_id
            self.px4_logs[drone_id] = Px4LogObserver(instance_id).start()
        observer = TelemetryObserver(drone_id).start()
        self.observers.append(observer)
        self.wait_ready(drone_id)
        return response

    def wait_ready(self, drone_id: str, timeout=READY_TIMEOUT):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
            if last.get("ready") and last.get("telemetry_fresh") and last.get("armed") is False:
                return last
            time.sleep(.5)
        raise AssertionError(f"PX4 did not become flight-ready in {timeout}s: {last}")

    def wait_execution(self, drone_id, execution_id, final, timeout=MISSION_TIMEOUT):
        deadline = time.monotonic() + timeout
        history = []
        while time.monotonic() < deadline:
            state = self.request("GET", f"/api/v1/drones/{drone_id}/flight/executions/{execution_id}").json()
            history.append(state)
            if state["status"] == final:
                return state, history
            if state["status"] in {"failed", "interrupted"}:
                raise AssertionError(f"Execution ended {state}")
            time.sleep(.25)
        raise AssertionError(f"Execution timed out; last state={history[-1] if history else None}")

    def wait_landed(self, drone_id, station, timeout=RETURN_TIMEOUT):
        deadline = time.monotonic() + timeout
        stable_since = None
        latest = None
        while time.monotonic() < deadline:
            latest = self.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
            armed = latest.get("armed")
            landed = latest.get("landed_state")
            telemetry = self.observers[self.drones.index(drone_id)].telemetry(timeout=2)
            velocity = telemetry.get("velocity", {})
            speed = (sum(float(velocity.get(axis, 0)) ** 2 for axis in ("north_m_s", "east_m_s", "down_m_s"))) ** .5
            landed_value = landed.get("landed_state") if isinstance(landed, dict) else landed
            on_ground = str(landed_value).lower() in {"on_ground", "2"}
            if armed is False and on_ground and speed < .2:
                position = telemetry.get("position", {})
                if position.get("latitude_deg") is not None and station.get("latitude_deg") is not None:
                    horizontal = math.hypot(
                        (position["latitude_deg"] - station["latitude_deg"]) * 111320,
                        (position["longitude_deg"] - station["longitude_deg"]) * 111320 *
                        math.cos(math.radians(station["latitude_deg"])))
                    distance = math.hypot(horizontal,
                        float(position.get("absolute_altitude_m", station["absolute_altitude_m"])) -
                        station["absolute_altitude_m"])
                    assert distance <= 3, f"RTL landed {distance:.1f}m from station"
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 2:
                    return latest
            else:
                stable_since = None
            time.sleep(.2)
        raise AssertionError(f"Confirmed landing was not observed: {latest}")

    def native_geodetic(self, xyz):
        context = self.geo_context
        raw = subprocess.check_output(["/tmp/native-geodetic",
            str(context["latitude_deg"]), str(context["longitude_deg"]),
            str(context["elevation"]), str(context["heading_deg"]),
            str(xyz["x"]), str(xyz["y"]), str(xyz["z"])], text=True)
        latitude, longitude, altitude = map(float, raw.split())
        return {"latitude_deg": latitude, "longitude_deg": longitude,
                "absolute_altitude_m": altitude}

    def mission(self, name, waypoints, **overrides):
        world = self.request("GET", "/api/v1/world").json()
        body = {"name": name, "world": world["name"], "waypoints": waypoints,
                "cruise_speed_m_s": 2, "takeoff_height_m": 5, "return_height_m": 8}
        body.update(overrides)
        return self.request("POST", "/api/v1/missions/", expected=(201,), json=body).json()

    def hub_telemetry(self, instance_id):
        observer = HubTelemetryObserver(instance_id).start()
        self.hub_observers.append(observer)
        return observer

    def observe_navsat(self, topic):
        observer = GazeboNavSatObserver(topic)
        self.navsat_observers.append(observer)
        return observer

    @staticmethod
    def request_backend_restart(scenario, timeout=60):
        """Ask the host runner to stop/start Backend; Docker stays on the host."""
        control = ARTIFACTS / "control"
        control.mkdir(parents=True, exist_ok=True)
        request = control / f"backend-loss-{scenario}.request"
        done = control / f"backend-loss-{scenario}.done"
        request.write_text(scenario)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if done.exists():
                request.unlink(missing_ok=True)
                done.unlink(missing_ok=True)
                return
            time.sleep(.1)
        raise AssertionError(f"Host runner did not restart Backend for {scenario} within {timeout}s")

    @staticmethod
    def request_mavsdk_kill(mavsdk_pid, px4_pid, scenario, timeout=30):
        """Ask the host runner to kill only mavsdk_server and confirm PX4 survives."""
        if not all(isinstance(pid, int) and pid > 1 for pid in (mavsdk_pid, px4_pid)):
            raise AssertionError("Invalid Hub process IDs for MAVSDK failure injection")
        control = ARTIFACTS / "control"
        control.mkdir(parents=True, exist_ok=True)
        request = control / f"mavsdk-kill-{scenario}.request"
        done = control / f"mavsdk-kill-{scenario}.done"
        request.write_text(f"{mavsdk_pid}:{px4_pid}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if done.exists():
                result = done.read_text().strip()
                request.unlink(missing_ok=True)
                done.unlink(missing_ok=True)
                if result != "px4_alive":
                    raise AssertionError(f"MAVSDK host failure injection failed: {result}")
                return
            time.sleep(.1)
        raise AssertionError(f"Host runner did not terminate MAVSDK for {scenario} within {timeout}s")

    @staticmethod
    def request_px4_probe(px4_pid, scenario, timeout=30):
        if not isinstance(px4_pid, int) or px4_pid <= 1:
            raise AssertionError("Invalid PX4 process ID for liveness probe")
        control = ARTIFACTS / "control"
        control.mkdir(parents=True, exist_ok=True)
        request = control / f"px4-probe-{scenario}.request"
        done = control / f"px4-probe-{scenario}.done"
        request.write_text(str(px4_pid))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if done.exists():
                result = done.read_text().strip()
                request.unlink(missing_ok=True)
                done.unlink(missing_ok=True)
                if result != "px4_alive":
                    raise AssertionError(f"PX4 process is not alive: {result}")
                return
            time.sleep(.1)
        raise AssertionError(f"Host runner did not probe PX4 PID {px4_pid} within {timeout}s")

    @staticmethod
    def archive_live_px4_instances(label, known_instances):
        """Retain diagnostics while a startup attempt may remove its failed instance."""
        root = Path(os.getenv("PX4_INSTANCE_ARTIFACTS", "/px4-instances"))
        if not root.exists():
            return
        for instance_root in root.iterdir():
            if not instance_root.is_dir() or instance_root.name in known_instances:
                continue
            destination = ARTIFACTS / "px4" / label / instance_root.name
            for source in instance_root.rglob("*"):
                if not source.is_file() or not (source.suffix == ".ulg" or source.name in {"px4.log", "mavsdk.log"}):
                    continue
                target = destination / source.relative_to(instance_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if source.name == "px4.log":
                        FlightEnvironment._filter_px4_log(source, target.with_suffix(".filtered.txt"),
                                                          max_bytes=8 * 1024 * 1024)
                    else:
                        shutil.copy2(source, target)
                except (FileNotFoundError, PermissionError, OSError):
                    # PX4 may rotate or remove a file while this diagnostic
                    # snapshot is being copied; the next pass retries it.
                    continue

    def dump(self, label: str, extra=None):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        archived_px4 = {}
        root = Path(os.getenv("PX4_INSTANCE_ARTIFACTS", "/px4-instances"))
        for drone_id, instance_id in self.instance_ids.items():
            if instance_id in self.archived_instances:
                archived_px4[drone_id] = {"instance_id": instance_id, "files": "archived by earlier snapshot"}
                continue
            instance_root = root / instance_id
            if not instance_root.exists():
                archived_px4[drone_id] = {"instance_id": instance_id, "error": "instance files unavailable"}
                continue
            destination = ARTIFACTS / "px4" / label / instance_id
            copied = []
            for source in instance_root.rglob("*"):
                if not source.is_file():
                    continue
                if source.suffix == ".ulg" or source.name == "mavsdk.log":
                    target = destination / source.relative_to(instance_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append({"path": str(target.relative_to(ARTIFACTS)), "bytes": target.stat().st_size})
                    if source.suffix == ".ulg":
                        try:
                            from pyulog import ULog
                            parsed = ULog(str(target))
                            topics = {item.name: item.data for item in parsed.data_list}
                            result = topics.get("mission_result", {})
                            fields = ("seq_current", "seq_reached", "seq_total", "finished", "failure", "warning")
                            summary = {
                                "parser": "pyulog==1.2.3",
                                "messages": [{"timestamp": item.timestamp,
                                    "level": item.log_level_str(), "message": item.message}
                                    for item in parsed.logged_messages],
                                "mission_result_tail": {key: result[key][-20:].tolist()
                                    for key in fields if key in result},
                            }
                            summary_path = target.with_suffix(".summary.json")
                            summary_path.write_text(json.dumps(summary, indent=2, default=str))
                            copied.append({"path": str(summary_path.relative_to(ARTIFACTS)),
                                           "bytes": summary_path.stat().st_size})
                        except Exception as exc:
                            copied.append({"ulog_parse_error": repr(exc)})
                elif source.name == "px4.log":
                    target = destination / "px4.log.filtered.txt"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    size = self._filter_px4_log(source, target, max_bytes=8 * 1024 * 1024)
                    copied.append({"path": str(target.relative_to(ARTIFACTS)), "bytes": size,
                                   "source_bytes": source.stat().st_size, "filtered": True})
            self.archived_instances.add(instance_id)
            archived_px4[drone_id] = {"instance_id": instance_id, "files": copied}
        document = {"label": label, "monotonic": time.monotonic(), "extra": self._redact(extra),
                    "drones": self.drones, "world": self.request("GET", "/api/v1/world").json(),
                    "status": self.request("GET", "/api/v1/system/status").json(),
                    "commands": self.command_log,
                    "telemetry": {obs.drone_id: obs.records for obs in self.observers},
                    "px4_process_logs": {drone_id: observer.records
                                         for drone_id, observer in self.px4_logs.items()},
                    "px4_archives": archived_px4,
                    "gazebo_poses": self.gazebo.records}
        path = ARTIFACTS / f"{int(time.time())}-{label}.json"
        path.write_text(json.dumps(document, indent=2, default=str))
        return path

    @staticmethod
    def _filter_px4_log(source: Path, target: Path, max_bytes: int) -> int:
        ansi = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
        useful = re.compile(rb"(?:INFO|WARN|ERROR|CRITICAL|DEBUG)\s+\[[^]]+\]")
        pending = b""
        written = 0
        truncated = False
        with source.open("rb") as src, target.open("wb") as dst:
            while chunk := src.read(65536):
                pending += chunk
                lines = re.split(rb"[\r\n]+", pending)
                pending = lines.pop()
                for line in lines:
                    clean = ansi.sub(b"", line)
                    if not useful.search(clean):
                        continue
                    output = clean.strip() + b"\n"
                    if written + len(output) > max_bytes:
                        truncated = True
                        break
                    dst.write(output)
                    written += len(output)
                if truncated:
                    break
            if not truncated and pending:
                clean = ansi.sub(b"", pending)
                if useful.search(clean):
                    output = clean.strip() + b"\n"
                    if written + len(output) <= max_bytes:
                        dst.write(output)
                        written += len(output)
                    else:
                        truncated = True
            if truncated:
                marker = b"\n[PX4 process log filtered at 8 MiB]\n"
                dst.write(marker)
                written += len(marker)
        return written

    def close(self):
        # Cleanup is best-effort; it never converts a failed flight into success.
        for drone_id in reversed(self.drones):
            try:
                current = self.request("GET", f"/api/v1/drones/{drone_id}/flight", timeout=5).json()
                if current.get("armed") is True:
                    self.request("POST", f"/api/v1/drones/{drone_id}/flight/return", expected=(202, 409),
                                 json={"request_id": f"cleanup-{time.time_ns()}"})
                    if self.observers:
                        self.wait_landed(drone_id, self.stations[drone_id], timeout=RETURN_TIMEOUT)
                self.request("DELETE", f"/api/v1/drones/{drone_id}/", expected=(200, 204, 404))
            except Exception:
                pass
        # A Backend restart intentionally forgets its public drone registry,
        # while Hub keeps the autonomous PX4 instance alive. Drain those
        # orphans only after the test artifact has been written by the fixture.
        try:
            instances = httpx.get(HUB + "/api/v1/instances/", timeout=5).json()
            for instance in instances:
                instance_id = instance.get("id")
                if not instance_id:
                    continue
                state = httpx.get(f"{HUB}/api/v1/instances/{instance_id}/flight", timeout=5)
                if state.status_code == 200:
                    flight = state.json()
                    landed = flight.get("landed_state")
                    landed_value = landed.get("landed_state") if isinstance(landed, dict) else landed
                    if flight.get("armed") is not False or str(landed_value).lower() not in {"on_ground", "2"}:
                        continue
                elif state.status_code != 404:
                    continue
                httpx.delete(f"{HUB}/api/v1/instances/{instance_id}/", timeout=10)
        except Exception:
            pass
        try:
            gazebos = httpx.get("http://gazebo-service:8000/api/v1/drones/", timeout=5).json()
            for drone in gazebos:
                drone_id = drone.get("id")
                if drone_id:
                    httpx.delete(f"http://gazebo-service:8000/api/v1/drones/{drone_id}/", timeout=10)
        except Exception:
            pass
        try:
            status = httpx.get(BACKEND + "/api/v1/system/status", timeout=5).json()
            configuration = status.get("backend", {}).get("world_configuration", {})
            if configuration.get("reason") == "configured_world_contains_drone":
                self.request_backend_restart(f"cleanup-{time.time_ns()}")
        except Exception:
            pass
        for observer in self.observers:
            observer.close()
        for observer in self.hub_observers:
            observer.close()
        for observer in self.navsat_observers:
            observer.close()
        for observer in self.px4_logs.values():
            observer.close()
        try:
            self.gazebo.close()
        finally:
            self.http.close()
