import asyncio
import json
from types import SimpleNamespace

from app.log_mailbox import LogMailbox
from app.realtime import Realtime


def test_log_fifo_bounds_gap_and_clear():
    async def run():
        queue = LogMailbox(capacity=3)
        for i in range(5):
            queue.put_nowait({"type": "log", "message": str(i)})
        assert await queue.get() == {"type": "gap", "dropped": 2}
        assert [(await queue.get())["message"] for _ in range(3)] == ["2", "3", "4"]
        queue.put_nowait({"type": "log", "message": "old"})
        queue.clear()
        queue.put_nowait({"type": "invalidated"})
        assert await queue.get() == {"type": "invalidated"}
    asyncio.run(run())


class Upstream:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.closed = False
    def __aiter__(self): return self
    async def __anext__(self): return await self.messages.get()
    async def close(self): self.closed = True


def test_log_shared_upstream_no_replay_and_generation_cleanup():
    async def run():
        item = SimpleNamespace(id="drone", generation=0, status="ready")
        platform = SimpleNamespace(drones={"drone": item}, world_generation=0)
        realtime = Realtime(platform)
        upstreams = {}
        calls = []
        async def upstream(channel):
            calls.append(channel)
            stream = upstreams[channel] = Upstream()
            return stream, None if channel == "world.logs" else item
        realtime._upstream = upstream
        channel = "drone.drone.logs"
        first = await realtime.subscribe(channel)
        upstreams[channel].messages.put_nowait(json.dumps({"type": "log", "message": "first", "source": "px4"}))
        assert (await asyncio.wait_for(first.get(), 2))["data"]["message"] == "first"
        second = await realtime.subscribe(channel)
        assert len(second.values) == 0
        assert calls == [channel]
        for text in ("second", "third"):
            upstreams[channel].messages.put_nowait(json.dumps({"type": "log", "message": text}))
        assert [(await asyncio.wait_for(first.get(), 2))["data"]["message"] for _ in range(2)] == ["second", "third"]
        assert [(await asyncio.wait_for(second.get(), 2))["data"]["message"] for _ in range(2)] == ["second", "third"]
        world = await realtime.subscribe("world.logs")
        upstreams["world.logs"].messages.put_nowait(json.dumps({"type": "log", "message": "world"}))
        message = await asyncio.wait_for(world.get(), 2)
        assert message["drone_id"] is None
        assert message["runtime_generation"] is None
        upstreams[channel].messages.put_nowait(json.dumps({"type": "log", "message": "stale"}))
        await asyncio.sleep(.05)
        await realtime.invalidate_all()
        assert (await first.get())["type"] == "invalidated"
        assert (await second.get())["type"] == "invalidated"
        assert (await world.get())["type"] == "invalidated"
        assert not first.values and not realtime.channels
        assert all(stream.closed for stream in upstreams.values())
        renewed = await realtime.subscribe("world.logs")
        assert not renewed.values
        await realtime.unsubscribe("world.logs", renewed)
        assert upstreams["world.logs"].closed
        assert not realtime.channels
    asyncio.run(run())


def test_pose_channel_shares_upstream_and_preserves_source_freshness():
    async def run():
        item = SimpleNamespace(id="public-drone", gazebo_id="gazebo-drone", generation=4,
                               status="ready")
        platform = SimpleNamespace(drones={item.id: item}, world_generation=2)
        realtime = Realtime(platform)
        upstreams = {}
        async def upstream(channel):
            stream = upstreams[channel] = Upstream()
            return stream, item
        realtime._upstream = upstream
        channel = f"drone.{item.id}.pose"
        first = await realtime.subscribe(channel)
        second = await realtime.subscribe(channel)
        assert len(upstreams) == 1
        source = {"type": "pose", "sequence": 42, "age_s": 0.01,
                  "world_generation": 3,
                  "received_at": "2026-10-05T00:00:00+00:00",
                  "pose": {"position": {"x": 1, "y": 2, "z": 3},
                           "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}
        upstreams[channel].messages.put_nowait(json.dumps(source))
        first_value, second_value = await asyncio.gather(
            asyncio.wait_for(first.get(), 2), asyncio.wait_for(second.get(), 2))
        assert first_value["drone_id"] == item.id
        assert first_value["runtime_generation"] == item.generation
        assert first_value["data"] == source
        assert second_value["data"]["sequence"] == 42
        await realtime.invalidate_drone(item.id)
        assert (await first.get())["type"] == "invalidated"
        assert (await second.get())["type"] == "invalidated"
        assert not realtime.channels
    asyncio.run(run())
