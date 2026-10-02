import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import quote
from websockets.asyncio.client import connect


class LatestByType:
    """Bounded mailbox: retain one newest envelope for each sample type."""
    def __init__(self):
        self.values = {}
        self.order = asyncio.Queue()
        self.ready = asyncio.Event()

    def put_nowait(self, value):
        key = value.get("type", "sample")
        if key not in self.values:
            self.order.put_nowait(key)
        self.values[key] = value
        self.ready.set()

    def get_nowait(self):
        key = self.order.get_nowait()
        value = self.values.pop(key)
        if not self.values:
            self.ready.clear()
        return value

    async def get(self):
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                await self.ready.wait()

    def clear(self):
        self.values.clear()
        while not self.order.empty():
            self.order.get_nowait()
        self.ready.clear()


class Realtime:
    """One upstream socket per subscribed channel; each client has a latest-value queue."""
    def __init__(self, platform):
        self.platform = platform
        self.channels = {}

    async def subscribe(self, channel):
        entry = self.channels.get(channel)
        if entry is None:
            upstream, resource = await self._upstream(channel)
            task = asyncio.create_task(self._pump(channel, upstream, resource))
            entry = self.channels[channel] = {"queues": set(), "upstream": upstream, "task": task,
                                               "generation": resource.generation}
        queue = LatestByType()
        entry["queues"].add(queue)
        return queue

    async def unsubscribe(self, channel, queue):
        entry = self.channels.get(channel)
        if entry is None: return
        entry["queues"].discard(queue)
        if not entry["queues"]:
            self.channels.pop(channel, None)
            entry["task"].cancel()
            await asyncio.gather(entry["task"], return_exceptions=True)

    async def close(self):
        entries = list(self.channels.values())
        self.channels.clear()
        for entry in entries: entry["task"].cancel()
        await asyncio.gather(*(entry["task"] for entry in entries), return_exceptions=True)

    async def invalidate_drone(self, drone_id):
        entries = [(channel, entry) for channel, entry in self.channels.items()
                   if channel.startswith(f"drone.{drone_id}.")]
        for channel, entry in entries:
            self.channels.pop(channel, None)
            for queue in tuple(entry["queues"]):
                queue.clear()
                queue.put_nowait({"channel": channel, "type": "invalidated",
                                  "world_generation": self.platform.world_generation})
            entry["task"].cancel()
        await asyncio.gather(*(entry["task"] for _, entry in entries), return_exceptions=True)

    async def invalidate_all(self):
        ids = set(self.platform.drones)
        for drone_id in ids:
            await self.invalidate_drone(drone_id)

    async def _upstream(self, channel):
        parts = channel.split(".")
        if len(parts) == 3 and parts[0] == "drone" and parts[2] == "telemetry":
            item = self.platform._record(parts[1])
            if not item.instance_id: raise ValueError("autopilot is stopped")
            base = self.platform.settings.hub_url.replace("http://", "ws://").replace("https://", "wss://")
            path = f"/api/v1/instances/{quote(item.instance_id)}/telemetry"
            return await connect(base + path, open_timeout=5), item
        if len(parts) == 4 and parts[0] == "drone" and parts[2] == "sensor":
            item = self.platform._record(parts[1])
            base = self.platform.settings.gazebo_url.replace("http://", "ws://").replace("https://", "wss://")
            path = f"/api/v1/drones/{quote(item.gazebo_id)}/sensors/{quote(parts[3])}/stream"
            return await connect(base + path, open_timeout=5), item
        raise ValueError("unsupported realtime channel")

    async def _pump(self, channel, upstream, resource):
        entry = self.channels.get(channel)
        try:
            async for raw in upstream:
                if entry is None or self.channels.get(channel) is not entry: return
                item = resource
                if item.id not in self.platform.drones or item.generation != entry["generation"]:
                    break
                payload = json.loads(raw)
                kind = payload.get("type") or payload.get("channel") or "sample"
                envelope = {"channel": channel, "type": kind, "drone_id": item.id,
                            "received_at": datetime.now(timezone.utc).isoformat(),
                            "world_generation": self.platform.world_generation,
                            "runtime_generation": item.generation, "data": payload}
                if payload.get("sim_time") is not None:
                    envelope["source_time"] = {"value": payload["sim_time"], "clock": "simulation"}
                elif payload.get("timestamp") is not None:
                    envelope["source_time"] = {"value": payload["timestamp"], "clock": "upstream_unspecified"}
                for queue in tuple(entry["queues"]):
                    queue.put_nowait(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            envelope = {"channel": channel, "type": "error", "message": str(exc),
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "world_generation": self.platform.world_generation}
            if entry:
                for queue in tuple(entry["queues"]):
                    queue.put_nowait(envelope)
        finally:
            await upstream.close()
            if entry and self.channels.get(channel) is entry:
                for queue in tuple(entry["queues"]):
                    queue.put_nowait({"channel": channel, "type": "invalidated", "world_generation": self.platform.world_generation})
                self.channels.pop(channel, None)
