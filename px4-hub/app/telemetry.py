import asyncio
import dataclasses
import enum
import math
from datetime import datetime, timezone


def to_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if dataclasses.is_dataclass(value):
        return {key: to_json(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, (tuple, list)):
        return [to_json(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: to_json(item) for key, item in vars(value).items() if not key.startswith("_")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


STREAMS = {
    "armed": "armed", "flight_mode": "flight_mode", "position": "position",
    "velocity": "velocity_ned", "attitude": "attitude_euler", "battery": "battery",
    "gps": "gps_info", "health": "health", "status_text": "status_text",
    "landed_state": "landed_state", "home": "home",
}


class LatestSamples:
    """One pending sample per type; replacement preserves fair delivery order."""

    def __init__(self):
        self.pending = {}
        self.available = asyncio.Event()
        self.closed = False

    def put(self, sample):
        if not self.closed:
            self.pending[sample["type"]] = sample
            self.available.set()

    async def get(self):
        await self.available.wait()
        if self.closed:
            return None
        name = next(iter(self.pending))
        sample = self.pending.pop(name)
        if not self.pending:
            self.available.clear()
        return sample

    def qsize(self):
        return len(self.pending)

    def close(self):
        self.closed = True
        self.pending.clear()
        self.available.set()


class TelemetryFanout:
    def __init__(self, system, instance_id: str):
        self.system = system
        self.instance_id = instance_id
        self.clients: set[LatestSamples] = set()
        self.tasks: list[asyncio.Task] = []
        self.closed = False
        self.connected: bool | None = None
        self.connection_received = asyncio.Event()
        self.last_received: float | None = None
        self.received_by_type: dict[str, float] = {}
        self.stream_errors: dict[str, str] = {}
        self.position_received = asyncio.Event()
        self.latest: dict[str, dict] = {}

    def start(self):
        self.tasks.append(asyncio.create_task(self._connection()))
        for name, method in STREAMS.items():
            self.tasks.append(asyncio.create_task(self._pump(name, getattr(self.system.telemetry, method)())))

    async def _connection(self):
        try:
            async for state in self.system.core.connection_state():
                self.connected = state.is_connected
                if self.connected:
                    self.connection_received.set()
            if not self.closed:
                self.connected = None
                self.stream_errors["connection"] = "Connection stream ended"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connected = None
            self.stream_errors["connection"] = str(exc)

    async def _pump(self, name, stream):
        try:
            async for value in stream:
                sample = {"instance_id": self.instance_id, "type": name,
                          "received_at": datetime.now(timezone.utc).isoformat(),
                          "data": to_json(value)}
                self.last_received = asyncio.get_running_loop().time()
                self.received_by_type[name] = self.last_received
                self.latest[name] = sample["data"]
                if name == "position":
                    self.position_received.set()
                for queue in tuple(self.clients):
                    queue.put(sample)
            if not self.closed:
                self.stream_errors[name] = "Telemetry stream ended"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stream_errors[name] = str(exc)

    def subscribe(self):
        queue = LatestSamples()
        if self.closed:
            queue.close()
        else:
            self.clients.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.clients.discard(queue)
        queue.close()

    async def close(self):
        self.closed = True
        self.connected = False
        for queue in tuple(self.clients):
            queue.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
