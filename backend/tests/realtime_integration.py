"""Live shared-upstream WebSocket check against the Compose stack."""
import asyncio
import json
import time
import httpx
from websockets.asyncio.client import connect

BACKEND = "http://backend:8003"
WS = "ws://backend:8003/api/v1/realtime"


async def receive(ws, predicate, timeout=15):
    async def read():
        while True:
            value = json.loads(await ws.recv())
            if value.get("type") == "error":
                raise AssertionError(f"Backend realtime error: {value}")
            if predicate(value):
                return value
    return await asyncio.wait_for(read(), timeout)


async def main():
    drone_id = None
    sockets = []
    async with httpx.AsyncClient(timeout=150) as client:
        try:
            response = await client.post(BACKEND + "/api/v1/drones/", json={
                "model": "x500_gimbal", "name": f"realtime_it_{int(time.time())}",
                "spawn_pad_id": "landing_pad_01",
            })
            response.raise_for_status()
            drone_id = response.json()["id"]
            channel = f"drone.{drone_id}.telemetry"
            first = await connect(WS)
            second = await connect(WS)
            sockets.extend([first, second])
            command = json.dumps({"action": "subscribe", "channels": [channel]})
            await first.send(command)
            await second.send(command)
            await receive(first, lambda msg: msg.get("type") == "subscribed")
            await receive(second, lambda msg: msg.get("type") == "subscribed")
            sample = await receive(first, lambda msg: msg.get("drone_id") == drone_id)
            assert sample["channel"] == channel and sample["runtime_generation"] >= 1
            # Leave the second client unread while its latest-value queues coalesce samples.
            await asyncio.sleep(2)
            first_sample = await receive(first, lambda msg: msg.get("drone_id") == drone_id)
            assert first_sample["type"] in {"position", "attitude", "health", "velocity", "battery", "gps", "armed", "flight_mode", "status_text"}
            await first.close()
            sockets.remove(first)
            second_sample = await receive(second, lambda msg: msg.get("drone_id") == drone_id)
            assert second_sample["runtime_generation"] == sample["runtime_generation"]
            deleted = await client.delete(BACKEND + f"/api/v1/drones/{drone_id}/")
            deleted.raise_for_status()
            invalidated = await receive(second, lambda msg: msg.get("type") == "invalidated")
            assert invalidated["channel"] == channel
            print("PASS backend realtime: shared clients, slow-reader coalescing, deletion invalidation")
        finally:
            for ws in sockets:
                await ws.close()
            if drone_id:
                await client.delete(BACKEND + f"/api/v1/drones/{drone_id}/")


if __name__ == "__main__":
    asyncio.run(main())
