"""PX4 flight execution and exclusive offboard control."""
import asyncio
import math
import secrets
import time
import uuid
from dataclasses import dataclass, field

from .errors import HubError

MAX_XY = 3.0
MAX_Z = 1.0
MAX_YAW = 60.0


@dataclass
class Execution:
    id: str
    request_id: str
    kind: str
    status: str = "preparing"
    phase: str = "validating"
    current: int = 0
    total: int = 0
    error: str | None = None
    task: asyncio.Task | None = None
    return_task: asyncio.Task | None = None


@dataclass
class OffboardSession:
    id: str
    token: str
    created: float = field(default_factory=time.monotonic)
    last_input: float = 0.0
    last_seq: int = -1
    owner_connected: bool = False
    armed: bool = False
    status: str = "ready"
    latest_input: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    watchdog: asyncio.Task | None = None


def _geo(context, x, y):
    """Convert Gazebo local ENU (including spherical heading) to WGS84."""
    if not context:
        raise HubError(409, "world_georeference_missing", "World has no active spherical coordinate origin")
    lat0 = math.radians(float(context["latitude_deg"]))
    lon0 = math.radians(float(context["longitude_deg"]))
    heading = math.radians(float(context.get("heading_deg", 0)))
    east = x * math.cos(heading) + y * math.sin(heading)
    north = -x * math.sin(heading) + y * math.cos(heading)
    lat = lat0 + north / 6378137.0
    lon = lon0 + east / (6378137.0 * max(1e-8, math.cos(lat0)))
    return math.degrees(lat), math.degrees(lon)


class FlightController:
    def __init__(self, service, record):
        self.service, self.record = service, record
        self.active: Execution | None = None
        self.executions: dict[str, Execution] = {}
        self.requests: dict[str, tuple[str, str]] = {}
        self.action_requests: dict[str, tuple[str, dict]] = {}
        self.session: OffboardSession | None = None
        self.original_rtl_altitude: float | None = None
        self.lock = asyncio.Lock()
        self.setpoint_lock = asyncio.Lock()

    def limits(self):
        params = self.record.saved_parameters or {}
        return {"horizontal_m_s": min(MAX_XY, params.get("MPC_XY_VEL_MAX", MAX_XY)),
                "up_m_s": min(MAX_Z, params.get("MPC_Z_VEL_MAX_UP", MAX_Z)),
                "down_m_s": min(MAX_Z, params.get("MPC_Z_VEL_MAX_DN", MAX_Z)),
                "yaw_deg_s": MAX_YAW}

    def telemetry(self):
        return self.record.telemetry.latest if self.record.telemetry else {}

    def state(self):
        samples = self.telemetry()
        return {"active": self.serialize(self.active) if self.active else None,
                "station": self.record.station_pose, "limits": self.limits(),
                "armed": samples.get("armed"), "landed_state": samples.get("landed_state"),
                "flight_mode": samples.get("flight_mode"),
                "telemetry_fresh": bool(self.record.telemetry and
                    self.record.telemetry.received_by_type.get("position") and
                    asyncio.get_running_loop().time() - self.record.telemetry.received_by_type["position"] <= self.service.settings.telemetry_stale_after),
                "offboard": self.session_public(self.session) if self.session else None,
                "ready": bool(self.record.status == "running" and self.record.binding_valid is True and
                    self.record.telemetry and self.record.telemetry.connected and
                    samples.get("health", {}).get("is_armable") and self._on_ground() and
                    samples.get("armed") is False),
                "available_actions": ["mission", "offboard", "return", "land"]}

    @staticmethod
    def serialize(execution):
        if execution is None: return None
        return {"execution_id": execution.id, "kind": execution.kind, "status": execution.status,
                "phase": execution.phase, "current_waypoint": execution.current,
                "total_waypoints": execution.total, "error": execution.error}

    @staticmethod
    def session_public(session):
        return {"session_id": session.id, "status": session.status, "armed": session.armed,
                "owner_connected": session.owner_connected}

    def _preflight(self, *, require_binding=True, require_armable=True):
        self.service._get_running(self.record.id)
        if require_binding and self.record.binding_valid is not True:
            raise HubError(409, "drone_binding_unverified", "Gazebo binding must be valid before flight", self.record.binding_error)
        if self.service.simulation_paused:
            raise HubError(409, "simulation_paused", "Flight control requires a running simulation")
        if not self.record.telemetry or not self.record.telemetry.connected:
            raise HubError(409, "px4_disconnected", "PX4 telemetry is not connected")
        if not self.state()["telemetry_fresh"]:
            raise HubError(409, "telemetry_stale", "Fresh position telemetry is required for flight control")
        health = self.telemetry().get("health", {})
        if require_armable and not health.get("is_armable"):
            raise HubError(409, "px4_not_armable", "PX4 health does not permit arming", health)

    async def validate_mission(self, mission):
        self._preflight()
        if mission["world"] != self.record.world:
            raise HubError(409, "mission_world_mismatch", "Mission world does not match PX4 instance world")
        geo = mission.get("coordinate_context")
        if not geo:
            raise HubError(409, "world_georeference_missing", "World has no active spherical coordinate origin")
        current_world = await self.service.gazebo.world()
        current_geo = current_world.get("spherical_coordinates")
        if current_world.get("name") != mission["world"] or current_geo != geo:
            raise HubError(409, "mission_world_context_changed", "Mission coordinate context no longer matches the active world",
                           {"mission": mission.get("coordinate_context"), "current": current_geo})
        position = self.telemetry().get("position", {})
        data = position
        station = self.record.station_pose
        if not station:
            raise HubError(409, "station_pose_unavailable", "Station pose was not captured for this drone")
        origin = geo
        points = []
        for waypoint in mission["waypoints"]:
            lat, lon = _geo(origin, waypoint["x"], waypoint["y"])
            points.append({"latitude_deg": lat, "longitude_deg": lon,
                           "absolute_altitude_m": float(geo.get("elevation", 0)) + waypoint["z"]})
        if any(point["absolute_altitude_m"] <= float(geo.get("elevation", 0)) + station["position"]["z"] for point in points):
            raise HubError(422, "waypoint_below_home", "Mission waypoints must be above station altitude")
        if not self._on_ground() or self.telemetry().get("armed") is not False:
            raise HubError(409, "drone_not_landed", "Mission start requires a disarmed drone on the ground")
        station_lat, station_lon = _geo(geo, station["position"]["x"], station["position"]["y"])
        current_lat, current_lon = data.get("latitude_deg"), data.get("longitude_deg")
        if current_lat is None or current_lon is None:
            raise HubError(409, "global_position_unavailable", "PX4 global position is required to verify the station")
        station_distance = math.hypot((current_lat-station_lat)*111320,
            (current_lon-station_lon)*111320*math.cos(math.radians(station_lat)))
        if station_distance > 3:
            raise HubError(409, "home_mismatch", "Observed PX4 position does not match current station",
                           {"distance_m": station_distance})
        home = self.telemetry().get("home")
        if not home or home.get("latitude_deg") is None or home.get("longitude_deg") is None or home.get("absolute_altitude_m") is None:
            raise HubError(409, "px4_home_unavailable", "PX4 home telemetry is required to verify the station")
        home_distance = math.hypot((home["latitude_deg"]-station_lat)*111320,
            (home["longitude_deg"]-station_lon)*111320*math.cos(math.radians(station_lat)),
            home["absolute_altitude_m"]-(float(geo.get("elevation", 0)) + station["position"]["z"]))
        if home_distance > 3:
            raise HubError(409, "home_mismatch", "Observed PX4 home does not match current station",
                           {"distance_m": home_distance})
        limits = self.limits()
        if mission["cruise_speed_m_s"] > limits["horizontal_m_s"]:
            raise HubError(422, "mission_speed_exceeds_limit", "Mission speed exceeds the active PX4 speed limit", limits)
        return {"valid": True, "world": self.record.world, "waypoints": points,
                "station": station, "limits": limits,
                "return": "PX4 return-to-launch and land"}

    async def start_mission(self, mission, request_id):
        async with self.lock:
            previous = self.requests.get(request_id)
            fingerprint = (mission["id"], str(mission["revision"]))
            if previous:
                if previous[0] != repr(fingerprint):
                    raise HubError(409, "request_id_conflict", "request_id was used for another mission")
                return self.serialize(self.executions[previous[1]])
            if self.active and self.active.status not in {"completed", "cancelled", "failed", "interrupted"}:
                raise HubError(409, "flight_mode_conflict", "Another flight operation is active")
            await self.validate_mission(mission)
            execution = Execution(str(uuid.uuid4()), request_id, "mission", total=len(mission["waypoints"]) + 2)
            self.active = execution
            self.executions[execution.id] = execution
            self.requests[request_id] = (repr(fingerprint), execution.id)
            execution.task = asyncio.create_task(self._run_mission(execution, mission))
            return self.serialize(execution)

    async def _run_mission(self, execution, mission):
        original_rtl_altitude = None
        try:
            from mavsdk_grpc.mission_raw import MissionItem
            execution.phase = "uploading"
            context = mission["coordinate_context"]
            items = [MissionItem(0, 2, 178, 0, 1, 1.0, mission["cruise_speed_m_s"], -1.0, 0.0, 0, 0, 0.0, 0)]
            for index, waypoint in enumerate(mission["waypoints"]):
                lat, lon = _geo(context, waypoint["x"], waypoint["y"])
                items.append(MissionItem(index + 1, 5, 16, 0, 1, 0.0, 1.0, 0.0, float("nan"),
                    round(lat * 1e7), round(lon * 1e7),
                    float(context.get("elevation", 0)) + waypoint["z"], 0))
            items.append(MissionItem(len(items), 2, 20, 0, 1, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0))
            await self.record.system.mission_raw.upload_mission(items)
            original_rtl_altitude = await self.record.system.param.get_param_float("RTL_RETURN_ALT")
            await self.record.system.param.set_param_float("RTL_RETURN_ALT", mission["return_height_m"])
            self.original_rtl_altitude = original_rtl_altitude
            execution.phase = "arming"
            await self.record.system.action.set_takeoff_altitude(mission["takeoff_height_m"])
            await self.record.system.action.arm()
            execution.phase = "takeoff"
            await self.record.system.action.takeoff()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                relative_altitude = self.telemetry().get("position", {}).get("relative_altitude_m", 0)
                if relative_altitude >= mission["takeoff_height_m"] * .8:
                    break
                await asyncio.sleep(.25)
            else:
                raise TimeoutError("PX4 takeoff altitude was not observed")
            execution.phase, execution.status = "mission", "active"
            await self.record.system.mission_raw.start_mission()
            async for progress in self.record.system.mission_raw.mission_progress():
                if self.active is not execution: return
                execution.current = min(max(progress.current - 1, 0), len(mission["waypoints"]))
                if progress.total and progress.current >= progress.total:
                    break
            execution.phase, execution.status = "returning", "returning"
            await self._wait_landed(execution)
            execution.status, execution.phase = "completed", "complete"
            await self.record.system.param.set_param_float("RTL_RETURN_ALT", original_rtl_altitude)
            self.original_rtl_altitude = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            execution.error = str(exc)
            execution.status, execution.phase = "failed", "failed"
            try: await self.record.system.action.return_to_launch()
            except Exception: pass
            if original_rtl_altitude is not None and not self._on_ground():
                try: await self._wait_landed(execution)
                except Exception: pass
            if original_rtl_altitude is not None and self._on_ground():
                try: await self.record.system.param.set_param_float("RTL_RETURN_ALT", original_rtl_altitude)
                except Exception: pass

    async def _wait_landed(self, execution):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.active is not execution: return
            samples = self.telemetry()
            armed = samples.get("armed")
            landed = samples.get("landed_state", {})
            state = str(landed.get("landed_state", landed.get("state", landed))).lower() if isinstance(landed, dict) else str(landed).lower()
            if armed is False and ("on_ground" in state or state == "2"):
                return
            await asyncio.sleep(.25)
        raise TimeoutError("PX4 landing was not confirmed")

    def execution(self, execution_id):
        result = self.executions.get(execution_id)
        if result is None: raise HubError(404, "execution_not_found", "Flight execution was not found")
        return self.serialize(result)

    async def cancel(self, execution_id):
        execution = self.executions.get(execution_id)
        if not execution: raise HubError(404, "execution_not_found", "Flight execution was not found")
        if execution.status in {"completed", "cancelled", "failed", "interrupted"}:
            return self.serialize(execution)
        if execution.return_task and not execution.return_task.done():
            return self.serialize(execution)
        execution.status, execution.phase = "returning", "returning"
        execution.return_task = asyncio.create_task(self._complete_return(execution, "cancelled"))
        return self.serialize(execution)

    async def _complete_return(self, execution, final_status):
        if execution.task and execution.task is not asyncio.current_task() and not execution.task.done():
            execution.task.cancel()
            await asyncio.gather(execution.task, return_exceptions=True)
        try:
            await self.record.system.action.return_to_launch()
            await self._wait_landed(execution)
            if self.original_rtl_altitude is not None:
                await self.record.system.param.set_param_float("RTL_RETURN_ALT", self.original_rtl_altitude)
                self.original_rtl_altitude = None
            execution.status, execution.phase = final_status, final_status
        except Exception as exc:
            execution.status, execution.error = "failed", str(exc)
            execution.phase = "failed"
        return self.serialize(execution)

    async def action(self, action, request_id):
        async with self.lock:
            previous = self.action_requests.get(request_id)
            if previous:
                if previous[0] != action:
                    raise HubError(409, "request_id_conflict", "request_id was used for another flight command")
                return previous[1]
            # Return and land remain available after Gazebo binding or arming health is lost in flight.
            self._preflight(require_binding=False, require_armable=False)
            if self.active and self.active.status not in {"completed", "cancelled", "failed", "interrupted"}:
                if action == "return":
                    result = await self.cancel(self.active.id)
                    self.action_requests[request_id] = (action, result)
                    return result
                if self.active.task: self.active.task.cancel()
                self.active.status, self.active.phase = "interrupted", "interrupted"
            if self.session and self.session.armed:
                await self.record.system.offboard.stop()
                self.session.armed = False
                self.session.status = "returning" if action == "return" else "landing"
            if action == "return": await self.record.system.action.return_to_launch()
            elif action == "land": await self.record.system.action.land()
            result = {"status": "returning" if action == "return" else "landing"}
            self.action_requests[request_id] = (action, result)
            return result

    async def create_session(self):
        async with self.lock:
            self._preflight()
            if not self._on_ground() or self.telemetry().get("armed") is not False:
                raise HubError(409, "drone_not_landed", "Offboard session must start with a disarmed drone on the ground")
            if self.active and self.active.status not in {"completed", "cancelled", "failed", "interrupted"}:
                raise HubError(409, "flight_mode_conflict", "Another flight operation is active")
            if self.session: raise HubError(409, "offboard_session_exists", "Offboard session already exists")
            self.session = OffboardSession(str(uuid.uuid4()), secrets.token_urlsafe(32), last_input=time.monotonic())
            self.session.watchdog = asyncio.create_task(self._watchdog(self.session))
            return {**self.session_public(self.session), "token": self.session.token,
                    "limits": self.limits(), "input_timeout_s": .5, "rtl_timeout_s": 5.0}

    async def session_action(self, session_id, action):
        async with self.lock:
            session = self._session(session_id)
            self._preflight()
            if action == "arm":
                if session.armed: return self.session_public(session)
                if not session.owner_connected:
                    raise HubError(409, "offboard_controller_required", "Connect the control WebSocket before arming")
                await self._send_velocity(0, 0, 0, 0)
                await asyncio.sleep(1.1)
                await self.record.system.action.arm()
                await self.record.system.offboard.start()
                session.armed, session.status, session.last_input = True, "active", time.monotonic()
            elif action == "disarm":
                if not self._on_ground(): raise HubError(409, "drone_airborne", "Disarm is allowed only on confirmed ground")
                if session.armed: await self.record.system.offboard.stop()
                await self.record.system.action.disarm()
                session.armed, session.status = False, "ready"
            return self.session_public(session)

    def _session(self, session_id):
        if not self.session or self.session.id != session_id:
            raise HubError(404, "offboard_session_not_found", "Offboard session was not found")
        return self.session

    def _on_ground(self):
        sample = self.telemetry().get("landed_state", {})
        value = str(sample.get("landed_state", sample.get("state", sample)) if isinstance(sample, dict) else sample).lower()
        return "on_ground" in value or value == "2"

    @staticmethod
    def _velocity(forward, right, up, yaw):
        from mavsdk_grpc.offboard import VelocityBodyYawspeed
        return VelocityBodyYawspeed(forward, right, -up, yaw * MAX_YAW)

    def _limited_velocity(self, forward, right, up, yaw):
        limits = self.limits()
        forward *= limits["horizontal_m_s"]
        right *= limits["horizontal_m_s"]
        horizontal = math.hypot(forward, right)
        if horizontal > limits["horizontal_m_s"]:
            scale = limits["horizontal_m_s"] / horizontal
            forward, right = forward * scale, right * scale
        up *= limits["up_m_s"] if up >= 0 else limits["down_m_s"]
        return self._velocity(forward, right, up, yaw)

    async def _send_velocity(self, forward, right, up, yaw):
        async with self.setpoint_lock:
            return await self.record.system.offboard.set_velocity_body(
                self._limited_velocity(forward, right, up, yaw))

    async def input(self, session_id, token, message):
        session = self._session(session_id)
        if not secrets.compare_digest(token, session.token):
            raise HubError(403, "invalid_session_token", "Offboard session token is invalid")
        if not session.armed: raise HubError(409, "offboard_not_armed", "Arm the offboard session before sending input")
        if self.service.simulation_paused:
            raise HubError(409, "simulation_paused", "Offboard input is not accepted while Gazebo is paused")
        seq = message.get("seq")
        values = [message.get(name) for name in ("forward", "right", "up", "yaw")]
        if session.status == "returning":
            raise HubError(409, "offboard_returning", "Control session has begun return-to-launch")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= session.last_seq or any(not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v) or abs(v) > 1 for v in values):
            raise HubError(422, "invalid_offboard_input", "Input sequence or axes are invalid")
        session.latest_input = tuple(float(value) for value in values)
        session.last_seq, session.last_input = seq, time.monotonic()
        if session.status == "input_lost_hold": session.status = "active"
        return {"type": "accepted", "seq": seq}

    async def _watchdog(self, session):
        try:
            while self.session is session:
                if session.armed and self.service.simulation_paused:
                    session.status = "paused"
                    await asyncio.sleep(.1)
                    continue
                if session.armed and session.status == "paused":
                    await self.record.system.offboard.stop()
                    await self.record.system.action.return_to_launch()
                    session.status = "returning"
                    session.armed = False
                    continue
                age = time.monotonic() - session.last_input
                if session.armed and session.status == "active" and age < .5:
                    await self._send_velocity(*session.latest_input)
                if session.armed and age >= .5 and session.status == "active":
                    if time.monotonic() - session.last_input >= .5:
                        await self._send_velocity(0, 0, 0, 0)
                        if time.monotonic() - session.last_input >= .5:
                            session.status = "input_lost_hold"
                if session.armed and age >= 5 and session.status == "input_lost_hold":
                    await self.record.system.offboard.stop()
                    await self.record.system.action.return_to_launch()
                    session.status = "returning"
                    session.armed = False
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            session.status = "failed"

    async def delete_session(self, session_id):
        async with self.lock:
            return await self._delete_session(session_id)

    async def _delete_session(self, session_id):
        session = self._session(session_id)
        if session.watchdog:
            session.watchdog.cancel()
            await asyncio.gather(session.watchdog, return_exceptions=True)
        if session.armed:
            if self._on_ground():
                try: await self.record.system.offboard.stop()
                except Exception: pass
                await self.record.system.action.disarm()
            else:
                try: await self.record.system.offboard.stop()
                finally: await self.record.system.action.return_to_launch()
        self.session = None
        return {"deleted": True, "session_id": session_id}

    async def close(self):
        if self.session:
            try: await self.delete_session(self.session.id)
            except Exception: pass
        if self.active and self.active.task and not self.active.task.done():
            self.active.status = "interrupted"
            self.active.task.cancel()
            await asyncio.gather(self.active.task, return_exceptions=True)
        if self.active and self.active.return_task and not self.active.return_task.done():
            self.active.status = "interrupted"
            self.active.return_task.cancel()
            await asyncio.gather(self.active.return_task, return_exceptions=True)
