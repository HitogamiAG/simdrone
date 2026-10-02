import asyncio
import uuid
from dataclasses import dataclass, field
import httpx

from .adapters import GazeboApi, HubApi
from .config import Settings
from .errors import BackendError


@dataclass
class Drone:
    id: str
    name: str
    model: str
    pose: dict
    gazebo_id: str | None = None
    instance_id: str | None = None
    status: str = "creating"
    autopilot_wanted: bool = True
    initial_sensor_rates: dict = field(default_factory=dict)
    generation: int = 0
    last_error: dict | None = None


class Platform:
    def __init__(self, settings=None, gazebo=None, hub=None):
        self.settings = settings or Settings()
        self.gazebo = gazebo or GazeboApi(self.settings.gazebo_url, self.settings.request_timeout)
        self.hub = hub or HubApi(self.settings.hub_url, max(self.settings.request_timeout, self.settings.startup_timeout))
        self.drones: dict[str, Drone] = {}
        self.lock = asyncio.Lock()
        self.world_generation = 0
        self.operation: str | None = None

    async def close(self):
        await asyncio.gather(self.gazebo.close(), self.hub.close())

    async def ready(self):
        try:
            world = await self.gazebo.world()
            configured_drones = await self.gazebo.drones()
            if configured_drones:
                return {"ready": False, "reason": "configured_world_contains_drone",
                        "drones": [d.get("name") for d in configured_drones]}
            return {"ready": True, "world": world["name"]}
        except Exception as exc:
            return {"ready": False, "reason": str(exc)}

    async def status(self):
        result = {"backend": {"ready": True, "operation": self.operation,
                               "world_configuration": getattr(self, "startup_world_check", None)},
                  "gazebo": {}, "px4_hub": {}, "mediamtx": {"available": False}}
        try:
            alive = await self.gazebo.call("GET", "/api/v1/server/server-alive/")
            result["gazebo"] = alive | {"available": True}
        except BackendError as exc:
            result["gazebo"] = {"available": False, "error": exc.code}
        if result["gazebo"].get("available"):
            try:
                models = await self.gazebo.drones()
                managed_models = {item.gazebo_id for item in self.drones.values() if item.gazebo_id}
                result["gazebo"]["unmanaged_drones"] = [model for model in models
                                                         if model.get("id") not in managed_models]
            except BackendError as exc:
                result["gazebo"]["drone_diagnostics_error"] = exc.code
        try:
            instances = await self.hub.instances()
            managed_instances = {item.instance_id for item in self.drones.values() if item.instance_id}
            result["px4_hub"] = {"available": True, "instances": len(instances),
                                  "unmanaged_instances": [entry for entry in instances
                                                          if entry.get("id") not in managed_instances]}
        except BackendError as exc:
            result["px4_hub"] = {"available": False, "error": exc.code}
        try:
            async with httpx.AsyncClient(base_url=self.settings.mediamtx_api_url, timeout=2,
                                         auth=(self.settings.mediamtx_user, self.settings.mediamtx_password)) as client:
                response = await client.get("/v3/paths/list")
                result["mediamtx"] = {"available": response.is_success}
        except Exception:
            result["mediamtx"] = {"available": False}
        return result

    async def _world(self):
        try:
            return await self.gazebo.world()
        except BackendError:
            raise

    async def _running(self):
        startup_check = getattr(self, "startup_world_check", None)
        if startup_check and startup_check.get("reason") == "configured_world_contains_drone":
            raise BackendError(503, "invalid_world_configuration", "Backend requires an initial world without drones", startup_check)
        world = await self._world()
        if world.get("simulation", {}).get("paused"):
            raise BackendError(409, "simulation_paused", "Operation requires a running simulation")
        return world

    async def _rates(self, gazebo_id):
        sensors = await self.gazebo.sensors(gazebo_id)
        return {sensor["id"]: sensor.get("update_rate") for sensor in sensors}

    async def _present_gazebo(self, item, gazebo=None):
        value = gazebo or await self.gazebo.drone(item.gazebo_id)
        for sensor in value.get("sensors", []):
            sensor["generation"] = item.generation
            if sensor.get("type") == "camera":
                sensor["video"] = await self.video(item.id, sensor["id"])
        return value

    def _serialize(self, item: Drone, gazebo=None, autopilot=None):
        return {"id": item.id, "name": item.name, "model": item.model, "status": item.status,
                "simulation": {"drone_id": item.gazebo_id,
                               "pose": (gazebo or {}).get("pose", item.pose),
                               "entity_id": (gazebo or {}).get("entity_id"),
                               "sensors": (gazebo or {}).get("sensors", [])},
                "autopilot": autopilot or {"status": "stopped" if not item.instance_id else "unknown"},
                "binding": {"valid": bool(gazebo and item.gazebo_id),
                            "generation": item.generation},
                "capabilities": {"flight_control": False, "missions": False, "manual_control": False},
                "last_error": item.last_error}

    async def list_drones(self):
        result = []
        for item in list(self.drones.values()):
            result.append(await self.get_drone(item.id))
        return result

    async def get_drone(self, drone_id):
        item = self.drones.get(drone_id)
        if not item:
            raise BackendError(404, "drone_not_found", "Drone was not found")
        gazebo = await self._present_gazebo(item) if item.gazebo_id else None
        px4 = await self.hub.get(item.instance_id) if item.instance_id else None
        if px4:
            item.autopilot_wanted = px4.get("status") not in {"stopped"}
        return self._serialize(item, gazebo, px4)

    async def create_drone(self, body):
        async with self.lock:
            self.operation = "create_drone"
            item = Drone(str(uuid.uuid4()), body.name or f"drone-{uuid.uuid4().hex[:8]}", body.model,
                         body.pose.model_dump())
            self.drones[item.id] = item
            gazebo = None
            try:
                await self._running()
                gazebo = await self.gazebo.create_drone({"model": item.model, "name": item.name, "pose": item.pose})
                item.gazebo_id = gazebo["id"]
                item.initial_sensor_rates = await self._rates(item.gazebo_id)
                px4 = await self.hub.create(item.gazebo_id)
                item.instance_id = px4["id"]
                item.status = "ready"
                item.generation += 1
                gazebo = await self._present_gazebo(item, gazebo)
                return self._serialize(item, gazebo, px4)
            except BaseException as exc:
                item.status = "failed"
                item.last_error = {"stage": "create", "message": str(exc),
                                   "gazebo_drone_id": item.gazebo_id, "px4_instance_id": item.instance_id}
                await self._cleanup_known(item)
                raise
            finally:
                self.operation = None

    async def _cleanup_known(self, item):
        failures = []
        if item.instance_id is None:
            try:
                instances = await self.hub.instances()
                orphan = next((entry for entry in instances
                               if entry.get("binding", {}).get("drone_id") == item.gazebo_id), None)
                if orphan: item.instance_id = orphan["id"]
            except Exception as exc:
                failures.append({"resource": "px4_lookup", "error": str(exc)})
        if item.instance_id:
            try: await self.hub.delete(item.instance_id)
            except Exception as exc: failures.append({"resource": "px4", "error": str(exc)})
            else: item.instance_id = None
        if item.gazebo_id:
            try: await self.gazebo.delete_drone(item.gazebo_id)
            except Exception as exc: failures.append({"resource": "gazebo", "error": str(exc)})
            else: item.gazebo_id = None
        if not failures:
            self.drones.pop(item.id, None)
        elif item.last_error is not None:
            item.last_error["cleanup_failures"] = failures

    async def delete_drone(self, drone_id):
        async with self.lock:
            item = self._record(drone_id)
            self.operation, item.status = "delete_drone", "deleting"
            try:
                if item.instance_id:
                    try: await self.hub.delete(item.instance_id)
                    except BackendError as exc:
                        if exc.status != 404: raise
                    item.instance_id = None
                if item.gazebo_id:
                    try: await self.gazebo.delete_drone(item.gazebo_id)
                    except BackendError as exc:
                        if exc.status != 404: raise
                self.drones.pop(item.id, None)
                return {"deleted": True, "id": drone_id}
            except Exception as exc:
                item.status, item.last_error = "failed", {"stage": "delete", "message": str(exc)}
                raise
            finally:
                self.operation = None

    async def reset_drone(self, drone_id):
        async with self.lock:
            item = self._record(drone_id)
            await self._running()
            self.operation, item.status = "drone_reset", "resetting"
            wanted = item.autopilot_wanted
            try:
                if item.instance_id:
                    await self.hub.delete(item.instance_id)
                    item.instance_id = None
                await self._invalidate(item)
                gazebo = await self.gazebo.reset_drone(item.gazebo_id)
                item.generation += 1
                if wanted:
                    px4 = await self.hub.create(item.gazebo_id)
                    item.instance_id = px4["id"]
                else:
                    px4 = None
                item.status = "ready"
                item.last_error = None
                gazebo = await self._present_gazebo(item, gazebo)
                return self._serialize(item, gazebo, px4)
            except Exception as exc:
                item.status, item.last_error = "failed", {"stage": "drone_reset", "message": str(exc)}
                raise
            finally:
                self.operation = None

    async def _invalidate(self, item):
        realtime = getattr(self, "realtime", None)
        if realtime: await realtime.invalidate_drone(item.id)

    def _record(self, drone_id):
        item = self.drones.get(drone_id)
        if item is None:
            raise BackendError(404, "drone_not_found", "Drone was not found")
        return item

    async def autopilot(self, drone_id):
        item = self._record(drone_id)
        return await self.hub.get(item.instance_id) if item.instance_id else {"status": "stopped", "instance": None}

    async def autopilot_action(self, drone_id, action):
        async with self.lock:
            item = self._record(drone_id)
            if action in {"start", "restart"}: await self._running()
            if action == "start" and item.instance_id:
                response = await self.hub.start(item.instance_id)
            elif action == "stop" and item.instance_id:
                response = await self.hub.stop(item.instance_id)
            elif action == "restart" and item.instance_id:
                response = await self.hub.restart(item.instance_id)
            elif action == "stop":
                return {"status": "stopped", "id": drone_id}
            elif action == "start":
                response = await self.hub.create(item.gazebo_id)
                item.instance_id = response["id"]
            elif action == "restart":
                response = await self.hub.create(item.gazebo_id)
                item.instance_id = response["id"]
            item.autopilot_wanted = action != "stop"
            item.generation += 1
            await self._invalidate(item)
            item.status = "ready"
            return response

    async def parameters(self, drone_id):
        item = self._record(drone_id)
        if not item.instance_id: raise BackendError(409, "autopilot_stopped", "Autopilot is stopped")
        return await self.hub.parameters(item.instance_id)

    async def patch_parameters(self, drone_id, body):
        async with self.lock:
            await self._running()
            item = self._record(drone_id)
            if not item.instance_id: raise BackendError(409, "autopilot_stopped", "Autopilot is stopped")
            return await self.hub.patch_parameters(item.instance_id, body)

    async def patch_sensor(self, drone_id, sensor_id, body):
        async with self.lock:
            await self._running()
            item = self._record(drone_id)
            before = await self.gazebo.sensor(item.gazebo_id, sensor_id)
            if "update_rate" not in before.get("capabilities", []):
                raise BackendError(422, "unsupported_field", "Sensor update_rate is not mutable")
            initial = item.initial_sensor_rates.get(sensor_id)
            value = body["update_rate"]
            if initial is not None and initial > 0 and (value <= 0 or value > initial):
                raise BackendError(422, "invalid_sensor_rate", "Rate must be positive and no greater than the initial rate", {"maximum": initial})
            self.operation, item.status = "sensor_patch", "resetting"
            wanted = item.autopilot_wanted
            try:
                if item.instance_id:
                    await self.hub.delete(item.instance_id)
                    item.instance_id = None
                await self._invalidate(item)
                await self.gazebo.reset_drone(item.gazebo_id)
                await self.gazebo.patch_sensor(item.gazebo_id, sensor_id, body)
                item.generation += 1
                if wanted:
                    px4 = await self.hub.create(item.gazebo_id)
                    item.instance_id = px4["id"]
                item.status, item.last_error = "ready", None
                return await self.gazebo.sensor(item.gazebo_id, sensor_id)
            except Exception as exc:
                item.status, item.last_error = "failed", {"stage": "sensor_patch", "message": str(exc)}
                raise
            finally:
                self.operation = None

    async def reset_sensor(self, drone_id, sensor_id):
        async with self.lock:
            item = self._record(drone_id)
            await self._running()
            await self.gazebo.sensor(item.gazebo_id, sensor_id)
            self.operation, item.status = "sensor_reset", "resetting"
            wanted = item.autopilot_wanted
            try:
                if item.instance_id:
                    await self.hub.delete(item.instance_id)
                    item.instance_id = None
                await self._invalidate(item)
                await self.gazebo.reset_drone(item.gazebo_id)
                item.generation += 1
                if wanted:
                    px4 = await self.hub.create(item.gazebo_id)
                    item.instance_id = px4["id"]
                item.status, item.last_error = "ready", None
                return await self.gazebo.sensor(item.gazebo_id, sensor_id)
            except Exception as exc:
                item.status, item.last_error = "failed", {"stage": "sensor_reset", "message": str(exc)}
                raise
            finally:
                self.operation = None

    async def sensors(self, drone_id):
        item = self._record(drone_id)
        gazebo = await self.gazebo.drone(item.gazebo_id)
        return (await self._present_gazebo(item, gazebo)).get("sensors", [])

    async def sensor(self, drone_id, sensor_id):
        item = self._record(drone_id)
        sensor = await self.gazebo.sensor(item.gazebo_id, sensor_id)
        sensor["generation"] = item.generation
        if sensor.get("type") == "camera": sensor["video"] = await self.video(drone_id, sensor_id)
        return sensor

    async def video(self, drone_id, sensor_id):
        item = self._record(drone_id)
        sensor = await self.gazebo.sensor(item.gazebo_id, sensor_id)
        if sensor.get("type") != "camera":
            raise BackendError(422, "not_a_camera", "Video is available only for camera sensors")
        path = f"drones/{item.gazebo_id}/sensors/{sensor_id}"
        return {"available": True, "active": sensor.get("active"),
                "url": f"{self.settings.media_url.rstrip('/')}/{path}/whep",
                "protocol": "WHEP", "on_demand": True}

    async def sensor_action(self, drone_id, sensor_id, action):
        item = self._record(drone_id)
        path = f"/api/v1/drones/{item.gazebo_id}/sensors/{sensor_id}/{action}"
        return await self.gazebo.call("POST", path, missing="sensor_not_found")

    async def _stop_all_instances(self):
        for instance in await self.hub.instances():
            try:
                await self.hub.delete(instance["id"])
                for item in self.drones.values():
                    if item.instance_id == instance["id"]:
                        item.instance_id = None
                        item.autopilot_wanted = False
            except BackendError as exc:
                if exc.status != 404: raise
                for item in self.drones.values():
                    if item.instance_id == instance["id"]:
                        item.instance_id = None
                        item.autopilot_wanted = False
        remaining = await self.hub.instances()
        if remaining:
            raise BackendError(503, "px4_cleanup_incomplete", "PX4 Hub still has instances", {"instances": [x["id"] for x in remaining]})

    async def _reset_world(self, reboot=False):
        try:
            await self._stop_all_instances()
            if getattr(self, "realtime", None): await self.realtime.invalidate_all()
            result = await (self.gazebo.reboot() if reboot else self.gazebo.world_reset())
            remaining = await self.gazebo.drones()
            if remaining:
                raise BackendError(409, "configured_world_contains_drone", "Reset world contains drone models",
                                   {"drones": [d.get("name") for d in remaining]})
            self.drones.clear()
            self.world_generation += 1
            return result
        except Exception as exc:
            for item in self.drones.values():
                item.status = "failed"
                item.last_error = {"stage": "world_reset", "message": str(exc),
                                   "gazebo_drone_id": item.gazebo_id,
                                   "px4_instance_id": item.instance_id,
                                   "resources_require_reconciliation": True}
            raise

    async def reset_world(self, reboot=False):
        async with self.lock:
            self.operation = "gazebo_reboot" if reboot else "world_reset"
            try:
                return await self._reset_world(reboot)
            finally:
                self.operation = None

    async def patch_world(self, body):
        async with self.lock:
            self.operation = "world_patch"
            current = await self._world()
            paused = current.get("simulation", {}).get("paused", False)
            merged = self._merge_world(current, body)
            try:
                await self._reset_world()
                if paused: await self.gazebo.pause()
                payload = self._world_payload(merged)
                result = await self.gazebo.patch_world(payload)
                remaining = await self.gazebo.drones()
                if remaining:
                    raise BackendError(409, "configured_world_contains_drone", "Reset world contains drone models", {"drones": [d.get("name") for d in remaining]})
                if not paused: await self.gazebo.resume()
                return {"applied": sorted(body), "world": result.get("world")}
            except Exception as exc:
                raise BackendError(503, "world_patch_failed", "World reset or settings application failed",
                                   {"stage": "reset_then_patch", "requested": body, "reason": str(exc)}) from exc
            finally:
                self.operation = None

    async def set_paused(self, paused):
        async with self.lock:
            self.operation = "pause" if paused else "resume"
            try:
                result = await (self.gazebo.pause() if paused else self.gazebo.resume())
                return {"paused": paused, "world": result}
            finally:
                self.operation = None

    @staticmethod
    def _merge_world(current, body):
        gravity = current.get("gravity") or {}
        if isinstance(gravity, (list, tuple)):
            gravity = dict(zip(("x", "y", "z"), gravity))
        merged = {"physics": dict(current.get("physics") or {}),
                  "gravity": dict(gravity),
                  "spherical_coordinates": dict(current.get("spherical_coordinates") or {})}
        merged["physics"].update(body.get("physics") or {})
        if body.get("gravity"): merged["gravity"] = body["gravity"]
        merged["spherical_coordinates"].update(body.get("spherical_coordinates") or {})
        return merged

    @staticmethod
    def _world_payload(values):
        physics = {key: value for key, value in values["physics"].items()
                   if key in {"max_step_size", "real_time_factor"}}
        spherical = {key: value for key, value in values["spherical_coordinates"].items()
                     if key in {"latitude_deg", "longitude_deg", "elevation", "heading_deg", "surface_model"}}
        return {key: value for key, value in {"physics": physics, "gravity": values["gravity"],
                 "spherical_coordinates": spherical}.items() if value}
