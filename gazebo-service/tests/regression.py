"""Real Gazebo regression checks for live state and reset lifecycle, run in Docker."""
import asyncio
import json
import math

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from app.gazebo.world import World


BASE = "http://127.0.0.1:8000"


def pose(x=0, y=0, z=3, yaw=0):
    return {"position": {"x": x, "y": y, "z": z},
            "orientation": {"x": 0, "y": 0, "z": math.sin(yaw/2), "w": math.cos(yaw/2)}}


async def checked(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    assert response.is_success, (path, response.status_code, response.text)
    return response.json()


async def expect_closed(ws):
    async def drain():
        while True:
            await ws.recv()
    try:
        await asyncio.wait_for(drain(), 4)
    except ConnectionClosed:
        return
    raise AssertionError("Old sensor WebSocket did not close")


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=45) as c:
        for _ in range(180):
            try:
                if (await c.get("/healthz")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.5)
        else:
            raise AssertionError("Service did not become ready")
        await checked(c, "POST", "/api/v1/world/world-reset")
        await checked(c, "POST", "/api/v1/world/pause")
        d = await checked(c, "POST", "/api/v1/drones/", json={"model": "test_quad", "name": "regression_quad", "pose": pose()})
        did = d["id"]
        path = f"/api/v1/drones/{did}/"
        entity_id = d["entity_id"]
        rejected = await c.patch(path, json={"pose": pose(2, 1, 4, .8)})
        assert rejected.status_code == 405
        changed = await checked(c, "GET", path)
        assert changed["entity_id"] == entity_id
        listed = await checked(c, "GET", "/api/v1/drones/")
        assert listed[0]["pose"] == changed["pose"]
        world = await checked(c, "GET", "/api/v1/world")
        assert next(m for m in world["models"] if m["name"] == d["name"])["pose"] == changed["pose"]
        print("PASS drone pose: PATCH removed; create pose and live GET/list/world remain", flush=True)

        # External Transport changes must be visible, and partial API PATCH must preserve them.
        w = World("empty", timeout_ms=6000)
        await asyncio.to_thread(w.wait_until_ready, 15000)
        await asyncio.to_thread(w.set_physics, max_step_size=.006, real_time_factor=.75, gravity=(0, 0, -8))
        await asyncio.to_thread(w.set_spherical_coordinates, latitude_deg=41, longitude_deg=72, elevation=400, heading_deg=12)
        world = await checked(c, "GET", "/api/v1/world")
        assert world["physics"] == {"max_step_size": .006, "real_time_factor": .75}
        assert world["gravity"] == [0, 0, -8]
        assert world["spherical_coordinates"]["heading_deg"] == 12
        changed = await checked(c, "PATCH", "/api/v1/world", json={"physics": {"max_step_size": .004}, "spherical_coordinates": {"latitude_deg": 42}})
        assert changed["world"]["physics"]["real_time_factor"] == .75
        assert changed["world"]["gravity"] == [0, 0, -8]
        assert changed["world"]["spherical_coordinates"]["longitude_deg"] == 72
        assert changed["world"]["simulation"]["paused"]
        print("PASS world: observed Transport changes, partial updates preserve neighbours", flush=True)
        await checked(c, "POST", "/api/v1/world/resume")
        # Give newly spawned sensor publishers time to advertise.
        for _ in range(50):
            sensors = await checked(c, "GET", path + "sensors/")
            if {"imu_sensor", "air_pressure_sensor", "forward_camera"}.issubset({s["id"] for s in sensors}):
                break
            await asyncio.sleep(.1)
        imu_uri = f"ws://127.0.0.1:8000{path}sensors/imu_sensor/stream"
        pressure_uri = f"ws://127.0.0.1:8000{path}sensors/air_pressure_sensor/stream"
        async with websockets.connect(imu_uri) as first, websockets.connect(imu_uri) as second, websockets.connect(pressure_uri) as pressure:
            for ws in (first, second, pressure):
                sample = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert sample["sim_time"]
            rate = await checked(c, "PATCH", path + "sensors/air_pressure_sensor/", json={"update_rate": 20})
            assert rate["update_rate"] == 20 and "confirmed" in rate["update_rate_source"]
            reset_sensor = await checked(c, "POST", path + "sensors/air_pressure_sensor/reset")
            assert reset_sensor["update_rate"] == 25 and reset_sensor["latest"] is None
            await checked(c, "POST", path + "sensors/forward_camera/activate")
            reset = await checked(c, "POST", path + "reset")
            assert reset["id"] == did and reset["entity_id"] != entity_id
            for ws in (first, second, pressure):
                await expect_closed(ws)
        camera = await checked(c, "GET", path + "sensors/forward_camera/")
        assert camera["active"] is True
        await checked(c, "POST", path + "sensors/forward_camera/deactivate")
        print("PASS sensor rate/reset, multi-client closure on drone-reset, active camera restoration", flush=True)
        async with websockets.connect(imu_uri) as ws:
            await asyncio.wait_for(ws.recv(), 5)
            await checked(c, "DELETE", path)
            await expect_closed(ws)
        assert (await c.get(path)).status_code == 404
        print("PASS delete: sensor WebSocket closed", flush=True)
        await checked(c, "POST", "/api/v1/world/world-reset")
        restored = await checked(c, "GET", "/api/v1/world")
        assert restored["physics"]["max_step_size"] == .004
        assert restored["physics"]["real_time_factor"] == 1
        assert restored["gravity"] == [0, 0, -9.80665]
        assert (await checked(c, "GET", "/api/v1/drones/")) == []
        print("Docker regression checks passed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
