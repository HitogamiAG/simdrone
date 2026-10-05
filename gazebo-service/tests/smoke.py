"""Container-level API, sensor, reset, and MediaMTX video smoke test."""
import asyncio
import subprocess
import time

import httpx
import websockets

BASE = "http://127.0.0.1:8000"


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        for _ in range(80):
            try:
                response = await client.get("/healthz")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
        assert response.status_code == 200, response.text
        assert (await client.get("/api/v1/server/server-alive/")).json()["world_ready"]

        world = (await client.get("/api/v1/world")).json()
        assert world["name"] == "empty"
        assert "scene" in world and "capabilities" in world
        before_paused = world["simulation"]["paused"]
        await client.post("/api/v1/world/pause")
        assert (await client.get("/api/v1/world")).json()["simulation"]["paused"] is True
        await client.post("/api/v1/world/resume")
        assert (await client.get("/api/v1/world")).json()["simulation"]["paused"] is False

        drone = (await client.post("/api/v1/drones/", json={"model": "test_quad", "name": "smoke_quad", "pose": {"position": {"x": 0, "y": 0, "z": 2}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}})).json()
        drone_id = drone["id"]
        assert (await client.get(f"/api/v1/drones/{drone_id}/")).status_code == 200
        patched = await client.patch(f"/api/v1/drones/{drone_id}/", json={"pose": {"position": {"x": 2, "y": 1, "z": 3}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}})
        assert patched.status_code == 405, patched.text
        sensor_list = (await client.get(f"/api/v1/drones/{drone_id}/sensors/")).json()
        assert any(s["id"] == "imu_sensor" for s in sensor_list), sensor_list
        assert any(s["id"] == "forward_camera" for s in sensor_list), sensor_list

        uri = f"ws://127.0.0.1:8000/api/v1/drones/{drone_id}/sensors/imu_sensor/stream"
        async with websockets.connect(uri, open_timeout=10) as ws:
            sample = await asyncio.wait_for(ws.recv(), timeout=15)
            assert '"type":"imu"' in sample or '"type": "imu"' in sample, sample[:300]

        rate = await client.patch(f"/api/v1/drones/{drone_id}/sensors/air_pressure_sensor/", json={"update_rate": 20})
        assert rate.status_code == 200, rate.text

        video = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "udp", "-i", f"rtsp://mediamtx:8554/drones/{drone_id}/sensors/forward_camera", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], capture_output=True, timeout=30)
        assert video.returncode == 0 and video.stdout.startswith(b"\xff\xd8"), video.stderr.decode(errors="replace")
        await asyncio.sleep(7)  # MediaMTX runOnUnDemand close timeout is five seconds.
        camera_state = (await client.get(f"/api/v1/drones/{drone_id}/sensors/forward_camera/")).json()
        assert camera_state["active"] is False, camera_state

        reset = await client.post(f"/api/v1/drones/{drone_id}/reset")
        assert reset.status_code == 200, reset.text
        assert reset.json()["id"] == drone_id
        assert (await client.delete(f"/api/v1/drones/{drone_id}/")).status_code == 200
        assert before_paused is False

        x500 = await client.post("/api/v1/drones/", json={"model": "x500_gimbal", "name": "smoke_x500"})
        assert x500.status_code == 201, x500.text
        x500_id = x500.json()["id"]
        expected_types = {"imu", "air_pressure", "magnetometer", "navsat", "camera"}
        for _ in range(20):
            x500_sensors = (await client.get(f"/api/v1/drones/{x500_id}/sensors/")).json()
            if expected_types.issubset({s["type"] for s in x500_sensors}):
                break
            await asyncio.sleep(.25)
        assert expected_types.issubset({s["type"] for s in x500_sensors})

        unsupported = await client.patch("/api/v1/world", json={"magnetic_field": {"x": 0, "y": 0, "z": 0}})
        assert unsupported.status_code == 422
        assert unsupported.json()["error"]["code"] == "invalid_input"
        changed = await client.patch("/api/v1/world", json={"physics": {"max_step_size": 0.005}})
        assert changed.status_code == 200, changed.text
        assert changed.json()["world"]["physics"]["max_step_size"] == 0.005
        reset_world = await client.post("/api/v1/world/world-reset")
        assert reset_world.status_code == 200, reset_world.text
        restored = (await client.get("/api/v1/world")).json()
        assert restored["physics"]["max_step_size"] == 0.001
        assert (await client.get("/api/v1/drones/")).json() == []

        before_reboot = (await client.get("/api/v1/server/server-alive/")).json()
        reboot = await client.post("/api/v1/server/server-reboot/")
        assert reboot.status_code == 200 and reboot.json()["world_ready"]
        after_reboot = (await client.get("/api/v1/server/server-alive/")).json()
        assert after_reboot["process_alive"] and after_reboot["world_ready"]
        assert before_reboot["pid"] != after_reboot["pid"]
        print("Docker smoke test passed: world, initial drone pose/reset, sensor stream/rate, H.264 RTSP")


if __name__ == "__main__":
    asyncio.run(main())
