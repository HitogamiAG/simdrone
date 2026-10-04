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
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563


class _MissionProgress:
    """One owned stream read, preserved across state polling timeouts."""

    def __init__(self, stream):
        self.stream = stream.__aiter__()
        self.pending = None

    async def wait(self, timeout=.5):
        if self.pending is None:
            self.pending = asyncio.create_task(anext(self.stream))
        done, _ = await asyncio.wait({self.pending}, timeout=timeout)
        if not done:
            return None
        try:
            return self.pending.result()
        finally:
            self.pending = None

    async def close(self):
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
        await self.stream.aclose()


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
    rtl_last_requested: float = 0.0
    watchdog: asyncio.Task | None = None


def _geo(context, x, y, z=0.0):
    """Convert Gazebo LOCAL2 coordinates to WGS84 geodetic coordinates."""
    if not context:
        raise HubError(409, "world_georeference_missing", "World has no active spherical coordinate origin")
    lat0 = math.radians(float(context["latitude_deg"]))
    lon0 = math.radians(float(context["longitude_deg"]))
    heading = math.radians(float(context.get("heading_deg", 0)))
    # Gazebo's LOCAL2 heading rotates local X toward geographic north.
    east = x * math.cos(heading) - y * math.sin(heading)
    north = x * math.sin(heading) + y * math.cos(heading)
    a, f = WGS84_A, WGS84_F
    b, e2 = a * (1-f), f * (2-f)
    sin_lat, cos_lat = math.sin(lat0), math.cos(lat0)
    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    ox = (n + float(context.get("elevation", 0))) * cos_lat * math.cos(lon0)
    oy = (n + float(context.get("elevation", 0))) * cos_lat * math.sin(lon0)
    oz = (n * (1-e2) + float(context.get("elevation", 0))) * sin_lat
    ex, ey = -math.sin(lon0), math.cos(lon0)
    nx, ny, nz = -sin_lat*math.cos(lon0), -sin_lat*math.sin(lon0), cos_lat
    ux, uy, uz = cos_lat*math.cos(lon0), cos_lat*math.sin(lon0), sin_lat
    X, Y, Z = ox + east*ex + north*nx + z*ux, oy + east*ey + north*ny + z*uy, oz + north*nz + z*uz
    p = math.hypot(X, Y)
    ep2 = (a*a-b*b)/(b*b)
    theta = math.atan2(Z*a, p*b)
    st, ct = math.sin(theta), math.cos(theta)
    lat = math.atan2(Z + ep2*b*st**3, p - e2*a*ct**3)
    lon = math.atan2(Y, X)
    altitude = p/math.cos(lat) - a/math.sqrt(1-e2*math.sin(lat)**2) if abs(math.cos(lat)) > 1e-10 else abs(Z)-b
    return math.degrees(lat), math.degrees(lon), altitude


def _mission_items(mission):
    """Build PX4 MissionRaw items with the first navigational waypoint active."""
    from mavsdk_grpc.mission_raw import MissionItem
    context = mission["coordinate_context"]
    items = [MissionItem(0, 2, 178, 0, 1, 1.0, mission["cruise_speed_m_s"], -1.0, 0.0, 0, 0, 0.0, 0)]
    for index, waypoint in enumerate(mission["waypoints"]):
        lat, lon, altitude = _geo(context, waypoint["x"], waypoint["y"], waypoint["z"])
        items.append(MissionItem(index + 1, 5, 16, 1 if index == 0 else 0, 1, 0.0, 1.0, 0.0, float("nan"),
            round(lat * 1e7), round(lon * 1e7), altitude, 0))
    items.append(MissionItem(len(items), 2, 20, 0, 1, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0))
    return items


def _mission_route_finished(current_waypoint, waypoint_count, flight_mode):
    """PX4 v1.16 marks its final RTL item current before MissionRaw finishes."""
    return (current_waypoint >= waypoint_count and
            str(flight_mode).upper() in {"RETURN_TO_LAUNCH", "RTL"})


class FlightController:
    def __init__(self, service, record):
        self.service, self.record = service, record
        self.active: Execution | None = None
        self.executions: dict[str, Execution] = {}
        self.requests: dict[str, tuple[str, str]] = {}
        self.action_requests: dict[str, tuple[str, dict]] = {}
        self.cancel_requests: dict[str, tuple[str, dict]] = {}
        self.session_requests: dict[str, tuple[str, str, dict]] = {}
        self.session_creates: dict[str, dict] = {}
        self.session: OffboardSession | None = None
        self.original_rtl_altitude: float | None = None
        self.lock = asyncio.Lock()
        self.setpoint_lock = asyncio.Lock()
        self.generation = str(uuid.uuid4())
        self.closed = False
        self.closed_event = asyncio.Event()

    def limits(self):
        params = self.record.saved_parameters or {}
        return {"horizontal_m_s": min(MAX_XY, params.get("MPC_XY_VEL_MAX", MAX_XY)),
                "up_m_s": min(MAX_Z, params.get("MPC_Z_VEL_MAX_UP", MAX_Z)),
                "down_m_s": min(MAX_Z, params.get("MPC_Z_VEL_MAX_DN", MAX_Z)),
                "yaw_deg_s": MAX_YAW}

    def telemetry(self):
        return self.record.telemetry.latest if self.record.telemetry else {}

    def telemetry_fresh(self, *names):
        fanout = self.record.telemetry
        if not fanout: return False
        now = asyncio.get_running_loop().time()
        return all((received := fanout.received_by_type.get(name)) is not None and
                   now - received <= self.service.settings.telemetry_stale_after for name in names)

    def state(self):
        samples = self.telemetry()
        received = self.record.telemetry.received_by_type.get("position") if self.record.telemetry else None
        telemetry_fresh = bool(received and asyncio.get_running_loop().time() - received <= self.service.settings.telemetry_stale_after)
        active = self.active and self.active.status not in {"completed", "cancelled", "failed", "interrupted"}
        ready = bool(self.record.status == "running" and self.record.binding_valid is True and
            self.record.telemetry and self.record.telemetry.connected and
            samples.get("health", {}).get("is_armable") and self._on_ground() and
            samples.get("armed") is False and telemetry_fresh and self.telemetry_fresh("armed", "landed_state") and not self.service.simulation_paused and not self.closed and
            not active and not self.session)
        actions = ["mission", "offboard"] if ready else []
        if samples.get("armed") is True:
            actions.extend(["return", "land"])
        return {"generation": self.generation, "active": self.serialize(self.active) if self.active else None,
                "station": self.record.station_pose, "limits": self.limits(),
                "armed": samples.get("armed"), "landed_state": samples.get("landed_state"),
                "flight_mode": samples.get("flight_mode"),
                "telemetry_fresh": telemetry_fresh,
                "offboard": self.session_public(self.session) if self.session else None,
                "ready": ready, "available_actions": actions}

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
            lat, lon, altitude = _geo(origin, waypoint["x"], waypoint["y"], waypoint["z"])
            points.append({"latitude_deg": lat, "longitude_deg": lon,
                           "absolute_altitude_m": altitude})
        station_lat, station_lon, station_alt = _geo(geo, station["position"]["x"], station["position"]["y"], station["position"]["z"])
        if any(point["absolute_altitude_m"] <= station_alt for point in points):
            raise HubError(422, "waypoint_below_home", "Mission waypoints must be above station altitude")
        if not self._ground_confirmed() or self.telemetry().get("armed") is not False:
            raise HubError(409, "drone_not_landed", "Mission start requires a disarmed drone on the ground")
        current_lat, current_lon = data.get("latitude_deg"), data.get("longitude_deg")
        if current_lat is None or current_lon is None or data.get("absolute_altitude_m") is None:
            raise HubError(409, "global_position_unavailable", "PX4 global position and altitude are required to verify the station")
        station_distance = math.hypot((current_lat-station_lat)*111320,
            (current_lon-station_lon)*111320*math.cos(math.radians(station_lat)),
            float(data["absolute_altitude_m"])-station_alt)
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

    def check_generation(self, expected):
        if expected != self.generation:
            raise HubError(409, "flight_generation_conflict", "Flight controller generation changed",
                           {"expected": expected, "current": self.generation})

    async def start_mission(self, mission, request_id, expected_generation=None):
        async with self.lock:
            if expected_generation is not None: self.check_generation(expected_generation)
            if self.closed:
                raise HubError(409, "flight_controller_closed", "Flight controller is stopping")
            previous = self.requests.get(request_id)
            fingerprint = (mission["id"], str(mission["revision"]), repr(mission))
            if previous:
                if previous[0] != repr(fingerprint):
                    raise HubError(409, "request_id_conflict", "request_id was used for another mission")
                return self.serialize(self.executions[previous[1]])
            if self.active and self.active.status not in {"completed", "cancelled", "failed", "interrupted"}:
                raise HubError(409, "flight_mode_conflict", "Another flight operation is active")
            if self.session:
                raise HubError(409, "flight_mode_conflict", "An offboard session is reserved")
            await self.validate_mission(mission)
            if self.closed:
                raise HubError(409, "flight_controller_closed", "Flight controller stopped during mission validation")
            execution = Execution(str(uuid.uuid4()), request_id, "mission", total=len(mission["waypoints"]) + 2)
            self.active = execution
            self.executions[execution.id] = execution
            self.requests[request_id] = (repr(fingerprint), execution.id)
            execution.task = asyncio.create_task(self._run_mission(execution, mission))
            return self.serialize(execution)

    async def _run_mission(self, execution, mission):
        original_rtl_altitude = None
        progress_reader = None
        try:
            execution.phase = "uploading"
            # PX4 requires MISSION_CURRENT to identify a navigational mission
            # item. DO_CHANGE_SPEED is an auxiliary command and cannot be the
            # current item (otherwise upload is rejected with CURRENT_INVALID).
            items = _mission_items(mission)
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
            if not await self._start_mission_mode(execution):
                return
            progress_reader = _MissionProgress(self.record.system.mission_raw.mission_progress())
            while True:
                if self.active is not execution: return
                # PX4 may leave MissionRaw progress on the final RTL item and
                # stop publishing once it has landed. Ground/disarmed at the
                # verified station is the authoritative completion signal.
                if self._landed_at_station():
                    if execution.current < len(mission["waypoints"]):
                        raise RuntimeError("PX4 returned to the station before completing all mission waypoints")
                    break
                if self._landed_and_disarmed():
                    raise RuntimeError("PX4 landed and disarmed away from the mission station")
                # PX4 v1.16 reports the terminal NAV_RETURN_TO_LAUNCH item as
                # current=N+1,total=N+2 and transitions to RTL without ever
                # reporting current==total. The final RTL item is success
                # only after every navigational waypoint has been reached.
                mode = self.telemetry().get("flight_mode", "")
                if _mission_route_finished(execution.current, len(mission["waypoints"]), mode):
                    break
                try:
                    progress = await progress_reader.wait()
                    if progress is None:
                        continue
                except StopAsyncIteration:
                    # Some PX4/MAVSDK combinations close this subscription
                    # between progress updates. Reconcile with the MAVSDK
                    # mission status RPC and subscribe again; stream closure
                    # alone does not mean that the flight ended.
                    if self._landed_at_station():
                        break
                    try:
                        if await self.record.system.mission_raw.is_mission_finished():
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(.25)
                    await progress_reader.close()
                    progress_reader = _MissionProgress(self.record.system.mission_raw.mission_progress())
                    continue
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
            if original_rtl_altitude is not None and not self._ground_confirmed():
                try: await self._wait_landed(execution)
                except Exception: pass
            if original_rtl_altitude is not None and self._ground_confirmed():
                try: await self.record.system.param.set_param_float("RTL_RETURN_ALT", original_rtl_altitude)
                except Exception: pass
        finally:
            if progress_reader is not None:
                await progress_reader.close()

    async def _start_mission_mode(self, execution):
        # MAVSDK start_mission only selects MISSION mode; repeating it does
        # not re-upload / rewind the route or arm again. PX4's completion of
        # TAKEOFF can race this command and leave the vehicle in HOLD.
        async with self.lock:
            if self.closed or self.active is not execution or execution.status != "active":
                return False
            await self.record.system.mission_raw.start_mission()
        deadline = time.monotonic() + 10
        next_retry = time.monotonic() + 1
        while time.monotonic() < deadline:
            async with self.lock:
                if self.closed or self.active is not execution or execution.status != "active":
                    return False
                fresh = self.telemetry_fresh("flight_mode", "armed", "landed_state")
                mode = str(self.telemetry().get("flight_mode", "")).upper()
                if fresh and mode == "MISSION":
                    return True
                if (fresh and mode == "HOLD" and self.telemetry().get("armed") is True
                        and not self._on_ground() and not self.service.simulation_paused
                        and time.monotonic() >= next_retry):
                    await self.record.system.mission_raw.start_mission()
                    next_retry = time.monotonic() + 1
            await asyncio.sleep(.1)
        raise RuntimeError("PX4 accepted mission start but did not enter MISSION mode")

    async def _wait_landed(self, execution):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.active is not execution: return
            if self._landed_at_station():
                return
            if self._landed_and_disarmed():
                raise RuntimeError("PX4 landed and disarmed away from the mission station")
            await asyncio.sleep(.25)
        raise TimeoutError("PX4 landing was not confirmed")

    def _landed_at_station(self):
        fanout = self.record.telemetry
        if not fanout: return False
        now = asyncio.get_running_loop().time()
        if any((received := fanout.received_by_type.get(kind)) is None or
               now - received > self.service.settings.telemetry_stale_after
               for kind in ("position", "armed", "landed_state")):
            return False
        samples = fanout.latest
        landed = samples.get("landed_state", {})
        state = str(landed.get("landed_state", landed.get("state", landed)) if isinstance(landed, dict) else landed).lower()
        position = samples.get("position", {})
        station = getattr(self.record, "station_pose", None)
        geo = getattr(self.record, "coordinate_context", None)
        if samples.get("armed") is not False or not ("on_ground" in state or state == "2") or not station or not geo:
            return False
        if (position.get("latitude_deg") is None or position.get("longitude_deg") is None or
                position.get("absolute_altitude_m") is None):
            return False
        lat, lon, altitude = _geo(geo, station["position"]["x"], station["position"]["y"], station["position"]["z"])
        distance = math.hypot((position["latitude_deg"]-lat)*111320,
            (position["longitude_deg"]-lon)*111320*math.cos(math.radians(lat)),
            float(position["absolute_altitude_m"])-altitude)
        return distance <= 3

    def _landed_and_disarmed(self):
        samples = self.telemetry()
        landed = samples.get("landed_state", {})
        value = landed.get("landed_state", landed.get("state", landed)) if isinstance(landed, dict) else landed
        state = str(value).lower()
        return (samples.get("armed") is False and
                ("on_ground" in state or state == "2") and
                self.telemetry_fresh("armed", "landed_state"))

    def execution(self, execution_id):
        result = self.executions.get(execution_id)
        if result is None: raise HubError(404, "execution_not_found", "Flight execution was not found")
        return self.serialize(result)

    async def cancel(self, execution_id, request_id=None, expected_generation=None):
        async with self.lock:
            if expected_generation is not None: self.check_generation(expected_generation)
            if self.closed: raise HubError(409, "flight_controller_closed", "Flight controller is stopping")
            return await self._cancel(execution_id, request_id)

    async def _cancel(self, execution_id, request_id=None):
        if request_id:
            previous = self.cancel_requests.get(request_id)
            if previous:
                if previous[0] != execution_id:
                    raise HubError(409, "request_id_conflict", "request_id was used for another cancellation")
                return previous[1]
        execution = self.executions.get(execution_id)
        if not execution: raise HubError(404, "execution_not_found", "Flight execution was not found")
        if execution.status in {"completed", "cancelled", "failed", "interrupted"}:
            result = self.serialize(execution)
            if request_id: self.cancel_requests[request_id] = (execution_id, result)
            return result
        if execution.return_task and not execution.return_task.done():
            result = self.serialize(execution)
            if request_id: self.cancel_requests[request_id] = (execution_id, result)
            return result
        execution.status, execution.phase = "returning", "returning"
        execution.return_task = asyncio.create_task(self._complete_return(execution, "cancelled"))
        result = self.serialize(execution)
        if request_id: self.cancel_requests[request_id] = (execution_id, result)
        return result

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

    async def action(self, action, request_id, expected_generation=None):
        async with self.lock:
            if expected_generation is not None: self.check_generation(expected_generation)
            if self.closed: raise HubError(409, "flight_controller_closed", "Flight controller is stopping")
            previous = self.action_requests.get(request_id)
            if previous:
                if previous[0] != action:
                    raise HubError(409, "request_id_conflict", "request_id was used for another flight command")
                return previous[1]
            # RTL/land must remain commandable when position telemetry goes stale in flight.
            if self.service.simulation_paused:
                raise HubError(409, "simulation_paused", "Flight commands are unavailable while the world is paused")
            self.service._get_running(self.record.id)
            if not self.record.telemetry or not self.record.telemetry.connected:
                raise HubError(409, "px4_disconnected", "PX4 telemetry is not connected")
            if self.active and self.active.status not in {"completed", "cancelled", "failed", "interrupted"}:
                if action == "return":
                    result = await self._cancel(self.active.id)
                    self.action_requests[request_id] = (action, result)
                    return result
            if action == "land" and self.active:
                # A previously accepted RTL must not continue after land was requested.
                self.active.status, self.active.phase = "interrupted", "interrupted"
                for task in (self.active.task, self.active.return_task):
                    if task and task is not asyncio.current_task() and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (self.active.task, self.active.return_task)
                                       if task and task is not asyncio.current_task()),
                                     return_exceptions=True)
            if self.session and self.session.armed:
                await self.record.system.offboard.stop()
                self.session.armed = False
                self.session.status = "returning" if action == "return" else "landing"
            if action == "return": await self.record.system.action.return_to_launch()
            elif action == "land": await self.record.system.action.land()
            result = {"status": "returning" if action == "return" else "landing"}
            self.action_requests[request_id] = (action, result)
            return result

    async def create_session(self, request_id=None, expected_generation=None):
        async with self.lock:
            if expected_generation is not None: self.check_generation(expected_generation)
            if self.closed: raise HubError(409, "flight_controller_closed", "Flight controller is stopping")
            if request_id and request_id in self.session_creates:
                return self.session_creates[request_id]
            self._preflight()
            if self.session and self.session.status == "returning" and self._landed_at_station():
                if self.session.watchdog:
                    self.session.watchdog.cancel()
                    await asyncio.gather(self.session.watchdog, return_exceptions=True)
                self.session = None
            if self.session:
                raise HubError(409, "offboard_session_exists", "Offboard session is already active or returning")
            if not self._ground_confirmed() or self.telemetry().get("armed") is not False:
                raise HubError(409, "drone_not_landed", "Offboard session must start with a disarmed drone on the ground")
            if self.active and self.active.status not in {"completed", "cancelled", "failed", "interrupted"}:
                raise HubError(409, "flight_mode_conflict", "Another flight operation is active")
            self.session = OffboardSession(str(uuid.uuid4()), secrets.token_urlsafe(32), last_input=time.monotonic())
            self.session.watchdog = asyncio.create_task(self._watchdog(self.session))
            result = {**self.session_public(self.session), "token": self.session.token,
                    "limits": self.limits(), "input_timeout_s": .5, "rtl_timeout_s": 5.0}
            if request_id: self.session_creates[request_id] = result
            return result

    async def session_action(self, session_id, action, request_id=None, expected_generation=None):
        async with self.lock:
            if expected_generation is not None: self.check_generation(expected_generation)
            if self.closed: raise HubError(409, "flight_controller_closed", "Flight controller is stopping")
            if request_id and request_id in self.session_requests:
                old_session, old_action, result = self.session_requests[request_id]
                if (old_session, old_action) != (session_id, action):
                    raise HubError(409, "request_id_conflict", "request_id was used for another offboard command")
                return result
            session = self._session(session_id)
            self._preflight()
            if action == "arm":
                if session.status in {"returning", "failed", "paused"} or not self._ground_confirmed() or self.telemetry().get("armed") is not False:
                    raise HubError(409, "offboard_session_unavailable", "Offboard session cannot arm unless the drone is disarmed on the ground")
                if session.armed: return self.session_public(session)
                if not session.owner_connected:
                    raise HubError(409, "offboard_controller_required", "Connect the control WebSocket before arming")
                await self._send_velocity(0, 0, 0, 0)
                await asyncio.sleep(1.1)
                if self.closed or self.service.simulation_paused or self.session is not session or session.status != "ready":
                    raise HubError(409, "offboard_session_unavailable", "Offboard preparation was interrupted before arming")
                command_started = asyncio.get_running_loop().time()
                arm_accepted = False
                try:
                    await self.record.system.action.arm()
                    arm_accepted = True
                    await self.record.system.offboard.start()
                except Exception:
                    # An arm command may have reached PX4 even when its response was lost.
                    try: await self.record.system.offboard.stop()
                    except Exception: pass
                    armed = True if arm_accepted else await self._armed_after(command_started, timeout=.75)
                    if armed is not False:
                        session.armed, session.status = True, "returning"
                        try: await self.record.system.action.return_to_launch()
                        except Exception: session.status = "failed"
                    else:
                        session.armed = False
                        session.status = "failed"
                    raise
                session.armed, session.status, session.last_input = True, "active", time.monotonic()
            elif action == "disarm":
                if not self._ground_confirmed(): raise HubError(409, "drone_airborne", "Disarm is allowed only on confirmed ground")
                if session.armed: await self.record.system.offboard.stop()
                await self.record.system.action.disarm()
                session.armed, session.status = False, "ready"
            result = self.session_public(session)
            if request_id: self.session_requests[request_id] = (session_id, action, result)
            return result

    def _session(self, session_id):
        if not self.session or self.session.id != session_id:
            raise HubError(404, "offboard_session_not_found", "Offboard session was not found")
        return self.session

    def _on_ground(self):
        sample = self.telemetry().get("landed_state", {})
        value = str(sample.get("landed_state", sample.get("state", sample)) if isinstance(sample, dict) else sample).lower()
        return "on_ground" in value or value == "2"

    def _ground_confirmed(self):
        return self.telemetry_fresh("landed_state", "armed") and self._on_ground()

    async def _armed_after(self, started_at, timeout):
        """Return an armed sample newer than a command, or None if outcome is unknown."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            fanout = self.record.telemetry
            received = fanout.received_by_type.get("armed") if fanout else None
            if (received is not None and received > started_at and
                    asyncio.get_running_loop().time() - received <= self.service.settings.telemetry_stale_after):
                value = fanout.latest.get("armed")
                if isinstance(value, bool):
                    return value
            await asyncio.sleep(.05)
        return None

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
        if self.closed: raise HubError(409, "flight_controller_closed", "Flight controller is stopping")
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
                if self.closed: return
                async with self.lock:
                    if self.session is not session or self.closed:
                        return
                    if session.status == "landing" and not session.armed and self._landed_and_disarmed():
                        session.owner_connected = False
                        self.session = None
                        return
                    if self.service.simulation_paused:
                        if not session.armed:
                            session.status = "paused"
                            session.owner_connected = False
                            self.session = None
                            return
                        session.status = "paused"
                        await self._send_velocity(0, 0, 0, 0)
                    elif session.armed and session.status == "paused":
                        await self.record.system.offboard.stop()
                        if self._ground_confirmed():
                            await self.record.system.action.disarm()
                            session.status, session.armed = "ready", False
                            session.owner_connected = False
                            self.session = None
                        else:
                            await self.record.system.action.return_to_launch()
                            session.status, session.armed = "returning", False
                    else:
                        # A successful MAVSDK RPC is not proof that PX4 changed
                        # mode. Keep requesting RTL until telemetry confirms it;
                        # this also covers a lost/consumed mode-command ACK after
                        # Offboard.stop() has first put PX4 into HOLD.
                        if session.status == "returning" and not session.armed:
                            mode = str(self.telemetry().get("flight_mode", "")).upper()
                            if (mode not in {"RETURN_TO_LAUNCH", "RTL"} and
                                    not self._landed_at_station() and
                                    time.monotonic() - session.rtl_last_requested >= 1.0):
                                await self.record.system.action.return_to_launch()
                                session.rtl_last_requested = time.monotonic()
                        age = time.monotonic() - session.last_input
                        if session.armed and session.status == "active" and age < .5:
                            await self._send_velocity(*session.latest_input)
                        elif session.armed and age >= .5 and session.status == "active":
                            await self._send_velocity(0, 0, 0, 0)
                            if self.session is session and session.status == "active" and time.monotonic() - session.last_input >= .5:
                                session.status = "input_lost_hold"
                        elif session.armed and age >= 5 and session.status == "input_lost_hold":
                            await self.record.system.offboard.stop()
                            if self.session is session and session.status == "input_lost_hold":
                                await self.record.system.action.return_to_launch()
                                session.status = "returning"
                                session.armed = False
                                session.rtl_last_requested = time.monotonic()
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self.lock:
                if self.session is not session:
                    return
                session.status = "failed"
                if session.armed:
                    try: await self.record.system.offboard.stop()
                    except Exception: pass
                    try:
                        await self.record.system.action.return_to_launch()
                        session.status = "returning"
                    except Exception:
                        session.status = "failed"
                    session.armed = False

    async def delete_session(self, session_id, expected_generation=None):
        async with self.lock:
            if expected_generation is not None: self.check_generation(expected_generation)
            return await self._delete_session(session_id)

    async def _delete_session(self, session_id):
        session = self._session(session_id)
        if session.watchdog and session.watchdog is not asyncio.current_task():
            session.watchdog.cancel()
            await asyncio.gather(session.watchdog, return_exceptions=True)
        if session.armed:
            if self._ground_confirmed():
                try: await self.record.system.offboard.stop()
                except Exception: pass
                await self.record.system.action.disarm()
            else:
                try: await self.record.system.offboard.stop()
                finally: await self.record.system.action.return_to_launch()
        self.session = None
        return {"deleted": True, "session_id": session_id}

    async def close(self):
        if self.closed: return
        self.closed = True
        self.closed_event.set()
        async with self.lock:
            if self.session:
                try: await self._delete_session(self.session.id)
                except Exception: pass
            if self.active and self.active.task and not self.active.task.done():
                self.active.status = "interrupted"
                self.active.task.cancel()
                await asyncio.gather(self.active.task, return_exceptions=True)
            if self.active and self.active.return_task and not self.active.return_task.done():
                self.active.status = "interrupted"
                self.active.return_task.cancel()
                await asyncio.gather(self.active.return_task, return_exceptions=True)
