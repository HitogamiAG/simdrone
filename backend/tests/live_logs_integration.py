"""Run sequentially in Docker; real Gazebo/PX4 output, no injected log lines."""
import asyncio
from contextlib import AsyncExitStack
import json
import httpx
from websockets.asyncio.client import connect

BASE = "http://backend:8003"
SOCKET = "ws://backend:8003/api/v1/realtime"


async def message(ws, kind, channel, timeout=30):
    async def receive():
        while True:
            payload = json.loads(await ws.recv())
            if payload.get("type") == "error":
                raise AssertionError(payload)
            if payload.get("type") == kind and payload.get("channel") == channel:
                return payload
    return await asyncio.wait_for(receive(), timeout)


async def subscribe(ws, channel):
    await ws.send(json.dumps({"action": "subscribe", "channels": [channel]}))
    await message(ws, "subscribed", channel)


async def run():
    async with httpx.AsyncClient(base_url=BASE, timeout=150) as http:
        async def call(method, path, **kwargs):
            response = await http.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        await call("POST", "/api/v1/world/reset")
        try:
            async with AsyncExitStack() as stack:
                world1 = await stack.enter_async_context(connect(SOCKET))
                world2 = await stack.enter_async_context(connect(SOCKET))
                await subscribe(world1, "world.logs")
                await subscribe(world2, "world.logs")
                # Creating a real camera produces Ogre2 visibility-mask warnings.
                drone = await call("POST", "/api/v1/drones/", json={"name": "live-log-check", "spawn_pad_id": "landing_pad_01"})
                gz1, gz2 = await asyncio.gather(message(world1, "log", "world.logs"), message(world2, "log", "world.logs"))
                assert gz1["data"]["source"] == gz2["data"]["source"] == "gazebo"
                assert gz1["data"]["message"] == gz2["data"]["message"]
                assert "\x1b" not in gz1["data"]["message"]
                print("PASS real Gazebo output reaches two Backend viewers", flush=True)
                channel = f"drone.{drone['id']}.logs"
                first = await stack.enter_async_context(connect(SOCKET))
                second = await stack.enter_async_context(connect(SOCKET))
                await subscribe(first, channel)
                await subscribe(second, channel)
                px1, px2 = await asyncio.gather(message(first, "log", channel), message(second, "log", channel))
                assert px1["data"]["source"] == px2["data"]["source"] == "px4"
                assert px1["drone_id"] == drone["id"]
                assert px1["data"]["message"] == px2["data"]["message"]
                print("PASS real PX4 stdout reaches two Backend viewers", flush=True)
                await call("POST", "/api/v1/world/pause")
                await asyncio.sleep(1)
                assert first.state.name == "OPEN" and second.state.name == "OPEN"
                await call("POST", "/api/v1/world/resume")
                await call("POST", f"/api/v1/drones/{drone['id']}/autopilot/stop")
                await asyncio.gather(message(first, "invalidated", channel), message(second, "invalidated", channel))
                print("PASS pause keeps log connections; stop invalidates both viewers", flush=True)
                await call("POST", f"/api/v1/drones/{drone['id']}/autopilot/start")
                await subscribe(first, channel)
                await call("POST", f"/api/v1/drones/{drone['id']}/autopilot/restart")
                await message(first, "invalidated", channel)
                await subscribe(first, channel)
                await call("DELETE", f"/api/v1/drones/{drone['id']}/")
                await message(first, "invalidated", channel)
                print("PASS start/restart creates new live stream; delete invalidates it", flush=True)
                await call("POST", "/api/v1/world/reset")
                await asyncio.gather(message(world1, "invalidated", "world.logs"), message(world2, "invalidated", "world.logs"))
                await subscribe(world1, "world.logs")
                await call("POST", "/api/v1/system/gazebo/reboot")
                await message(world1, "invalidated", "world.logs")
                print("PASS world reset/reboot invalidate old Gazebo stream", flush=True)
        finally:
            await call("POST", "/api/v1/world/reset")
        assert not await call("GET", "/api/v1/drones/")
        print("Live log integration passed", flush=True)


asyncio.run(run())
