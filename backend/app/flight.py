import asyncio
import uuid

from .errors import BackendError
from .missions import MissionStore


class FlightPlatform:
    """Backend ownership and persistence for public flight resources."""
    def __init__(self, platform):
        self.platform = platform
        self.store = MissionStore(platform.settings.mission_db)
        self.executions = {}
        self.request_ids = {}
        self.control_requests = {}

    def control_replay(self, drone_id, request_id, fingerprint):
        entry = self.control_requests.get((drone_id, request_id))
        if entry is None and (drone_id, request_id) in self.request_ids:
            raise BackendError(409, "request_id_conflict", "request_id was used for another operation")
        if entry is None: return None
        if entry["fingerprint"] != fingerprint:
            raise BackendError(409, "request_id_conflict", "request_id was used for another operation")
        return entry["response"]

    def remember_control(self, drone_id, request_id, fingerprint, response):
        self.control_requests[(drone_id, request_id)] = {"fingerprint": fingerprint, "response": response}

    def close(self):
        # The Hub owns active flight work; Backend shutdown only drops its process-local index.
        self.executions.clear()

    async def _world_context(self):
        world = await self.platform.gazebo.world()
        return {"name": world["name"], "spherical_coordinates": world.get("spherical_coordinates")}

    def _validate_body(self, body, context):
        if body.get("world") != context["name"]:
            raise BackendError(409, "mission_world_mismatch", "Mission belongs to another Gazebo world")
        return {"name": body["name"], "world": context["name"],
                "coordinate_context": context["spherical_coordinates"],
                "waypoints": body["waypoints"], "cruise_speed_m_s": body["cruise_speed_m_s"],
                "takeoff_height_m": body["takeoff_height_m"], "return_height_m": body["return_height_m"]}

    async def create_mission(self, body):
        context = await self._world_context()
        document = self._validate_body(body, context) | {"id": str(uuid.uuid4()), "revision": 1}
        return await asyncio.to_thread(self.store.create, document)

    async def update_mission(self, mission_id, body):
        current = await asyncio.to_thread(self.store.get, mission_id)
        if current is None:
            raise BackendError(404, "mission_not_found", "Mission was not found")
        context = await self._world_context()
        document = self._validate_body(body, context)
        result, error = await asyncio.to_thread(self.store.update, mission_id, body["expected_revision"], document)
        if error == "missing":
            raise BackendError(404, "mission_not_found", "Mission was not found")
        if error:
            raise BackendError(409, "mission_revision_conflict", "Mission revision changed", result)
        return result

    async def validate_mission(self, mission_id, drone_id, revision=None):
        mission = await asyncio.to_thread(self.store.get, mission_id)
        if not mission or (revision is not None and revision != mission["revision"]):
            raise BackendError(404 if not mission else 409, "mission_not_found" if not mission else "mission_revision_conflict", "Mission revision is unavailable")
        drone = self.platform._record(drone_id)
        if not drone.instance_id:
            raise BackendError(409, "autopilot_not_running", "Drone has no PX4 instance")
        result = await self.platform.hub.flight_validate(drone.instance_id, mission)
        return result

    async def start_mission(self, drone_id, body):
        # Serialize only acceptance with lifecycle operations; the Hub owns flight execution.
        async with self.platform.lock:
            request_id = body["request_id"]
            key = (drone_id, request_id)
            fingerprint = (body["mission_id"], body["revision"])
            if key in self.control_requests:
                raise BackendError(409, "request_id_conflict", "request_id was used for another operation")
            previous = self.request_ids.get(key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise BackendError(409, "request_id_conflict", "request_id was used for another operation")
                return previous["response"]
            await self.platform._running()
            drone = self.platform._record(drone_id)
            mission = await asyncio.to_thread(self.store.get, body["mission_id"])
            if mission is None:
                raise BackendError(404, "mission_not_found", "Mission was not found")
            if body.get("revision", mission["revision"]) != mission["revision"]:
                raise BackendError(409, "mission_revision_conflict", "Requested mission revision is stale")
            if not drone.instance_id:
                raise BackendError(409, "autopilot_not_running", "Drone has no PX4 instance")
            hub_state = await self.platform.hub.flight_state(drone.instance_id)
            accepted = await self.platform.hub.flight_start_mission(
                drone.instance_id, mission, request_id, hub_state["generation"])
            execution_id = accepted["execution_id"]
            response = {"execution_id": execution_id, "status": "preparing", "mission_id": mission["id"],
                        "revision": mission["revision"], "drone_id": drone_id}
            self.executions[execution_id] = {**response, "generation": drone.generation}
            self.request_ids[key] = {"fingerprint": fingerprint, "response": response}
            return response

    async def flight_status(self, drone_id):
        drone = self.platform._record(drone_id)
        if not drone.instance_id:
            return {"drone_id": drone_id, "active": None, "ready": False}
        state = await self.platform.hub.flight_state(drone.instance_id)
        return {"drone_id": drone_id, **state}

    async def execution(self, drone_id, execution_id):
        drone = self.platform._record(drone_id)
        state = await self.platform.hub.flight_execution(drone.instance_id, execution_id)
        return {"drone_id": drone_id, **state}

    async def cancel(self, drone_id, execution_id, request_id):
        fingerprint = ("mission_cancel", execution_id)
        replay = self.control_replay(drone_id, request_id, fingerprint)
        if replay is not None: return replay
        drone = self.platform._record(drone_id)
        state = await self.platform.hub.flight_state(drone.instance_id)
        result = await self.platform.hub.flight_cancel(drone.instance_id, execution_id, request_id, state["generation"])
        self.remember_control(drone_id, request_id, fingerprint, result)
        return result
