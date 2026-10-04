import httpx
from .errors import BackendError


class ServiceApi:
    def __init__(self, base_url, timeout):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def close(self):
        await self.client.aclose()

    async def call(self, method, path, *, json=None, missing="resource_not_found"):
        try:
            response = await self.client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise BackendError(503, "dependency_unavailable", "A platform service is unavailable",
                               {"service": str(self.client.base_url), "reason": str(exc)}) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:1000]}
        if response.status_code == 404:
            raise BackendError(404, missing, "Resource was not found", payload)
        if response.status_code == 409:
            err = payload.get("error", {})
            raise BackendError(409, err.get("code", "operation_conflict"),
                               err.get("message", "Operation conflicts with current state"), err.get("details"))
        if response.is_error:
            err = payload.get("error", {})
            raise BackendError(response.status_code if response.status_code < 500 else 503,
                               err.get("code", "upstream_error"), err.get("message", "Upstream request failed"),
                               {"upstream_status": response.status_code, "upstream_details": err.get("details", payload)})
        return payload


class GazeboApi(ServiceApi):
    async def world(self): return await self.call("GET", "/api/v1/world")
    async def drones(self): return await self.call("GET", "/api/v1/drones/")
    async def drone(self, drone_id): return await self.call("GET", f"/api/v1/drones/{drone_id}/", missing="drone_not_found")
    async def create_drone(self, body): return await self.call("POST", "/api/v1/drones/", json=body)
    async def delete_drone(self, drone_id): return await self.call("DELETE", f"/api/v1/drones/{drone_id}/", missing="drone_not_found")
    async def reset_drone(self, drone_id): return await self.call("POST", f"/api/v1/drones/{drone_id}/reset", missing="drone_not_found")
    async def world_reset(self): return await self.call("POST", "/api/v1/world/world-reset")
    async def reboot(self): return await self.call("POST", "/api/v1/server/server-reboot/")
    async def patch_world(self, body): return await self.call("PATCH", "/api/v1/world", json=body)
    async def pause(self): return await self.call("POST", "/api/v1/world/pause")
    async def resume(self): return await self.call("POST", "/api/v1/world/resume")
    async def sensors(self, drone_id): return await self.call("GET", f"/api/v1/drones/{drone_id}/sensors/", missing="drone_not_found")
    async def sensor(self, drone_id, sensor_id): return await self.call("GET", f"/api/v1/drones/{drone_id}/sensors/{sensor_id}/", missing="sensor_not_found")
    async def patch_sensor(self, drone_id, sensor_id, body): return await self.call("PATCH", f"/api/v1/drones/{drone_id}/sensors/{sensor_id}/", json=body, missing="sensor_not_found")
    async def reset_sensor(self, drone_id, sensor_id): return await self.call("POST", f"/api/v1/drones/{drone_id}/sensors/{sensor_id}/reset", missing="sensor_not_found")


class HubApi(ServiceApi):
    async def instances(self): return await self.call("GET", "/api/v1/instances/")
    async def create(self, drone_id): return await self.call("POST", "/api/v1/instances/", json={"drone_id": drone_id})
    async def get(self, instance_id): return await self.call("GET", f"/api/v1/instances/{instance_id}/", missing="autopilot_not_found")
    async def delete(self, instance_id): return await self.call("DELETE", f"/api/v1/instances/{instance_id}/", missing="autopilot_not_found")
    async def start(self, instance_id): return await self.call("POST", f"/api/v1/instances/{instance_id}/start")
    async def stop(self, instance_id): return await self.call("POST", f"/api/v1/instances/{instance_id}/stop")
    async def restart(self, instance_id): return await self.call("POST", f"/api/v1/instances/{instance_id}/restart")
    async def parameters(self, instance_id): return await self.call("GET", f"/api/v1/instances/{instance_id}/parameters/")
    async def patch_parameters(self, instance_id, body): return await self.call("PATCH", f"/api/v1/instances/{instance_id}/parameters/", json=body)
    async def flight_state(self, instance_id): return await self.call("GET", f"/api/v1/instances/{instance_id}/flight")
    async def flight_validate(self, instance_id, mission): return await self.call("POST", f"/api/v1/instances/{instance_id}/flight/missions/validate", json=mission)
    async def flight_start_mission(self, instance_id, mission, request_id, generation):
        return await self.call("POST", f"/api/v1/instances/{instance_id}/flight/missions", json={"mission": mission, "request_id": request_id, "expected_generation": generation})
    async def flight_execution(self, instance_id, execution_id): return await self.call("GET", f"/api/v1/instances/{instance_id}/flight/executions/{execution_id}")
    async def flight_cancel(self, instance_id, execution_id, request_id, generation=None): return await self.call("POST", f"/api/v1/instances/{instance_id}/flight/executions/{execution_id}/cancel", json={"request_id": request_id, "expected_generation": generation})
    async def flight_action(self, instance_id, action, body=None): return await self.call("POST", f"/api/v1/instances/{instance_id}/flight/{action}", json=body)
    async def create_offboard(self, instance_id, request_id, generation=None): return await self.call("POST", f"/api/v1/instances/{instance_id}/flight/offboard/sessions", json={"request_id": request_id, "expected_generation": generation})
    async def offboard_action(self, instance_id, session_id, action, request_id, generation=None): return await self.call("POST", f"/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}/{action}", json={"request_id": request_id, "expected_generation": generation})
    async def offboard_get(self, instance_id, session_id): return await self.call("GET", f"/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}")
    async def offboard_delete(self, instance_id, session_id, generation=None): return await self.call("DELETE", f"/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}", json={"expected_generation": generation})
    def websocket_url(self, instance_id, session_id):
        base = str(self.client.base_url).replace("https://", "wss://").replace("http://", "ws://")
        return f"{base.rstrip('/')}/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}/control"
