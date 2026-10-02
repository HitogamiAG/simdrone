import httpx
from .errors import HubError


class GazeboApi:
    def __init__(self, base_url: str):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=3)

    async def close(self):
        await self.client.aclose()

    async def drone(self, drone_id: str):
        try:
            response = await self.client.get(f"/api/v1/drones/{drone_id}/")
        except httpx.HTTPError as exc:
            raise HubError(503, "gazebo_unavailable", "Gazebo API is unavailable", str(exc)) from exc
        if response.status_code == 404:
            raise HubError(404, "drone_not_found", "Gazebo drone does not exist", {"drone_id": drone_id})
        if response.is_error:
            raise HubError(503, "gazebo_request_failed", "Gazebo API rejected the request", {"status": response.status_code})
        return response.json()

    async def world(self):
        try:
            response = await self.client.get("/api/v1/world")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HubError(503, "gazebo_unavailable", "Gazebo API is unavailable", str(exc)) from exc
        return response.json()

    async def simulation(self):
        world = await self.world()
        return world["name"], bool(world.get("simulation", {}).get("paused", False))
