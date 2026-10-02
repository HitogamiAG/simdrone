import asyncio
import dataclasses
import enum
from datetime import datetime, timezone


def to_json(value):
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
}


class TelemetryFanout:
    def __init__(self, system, instance_id: str):
        self.system = system
        self.instance_id = instance_id
        self.clients: set[asyncio.Queue] = set()
        self.tasks: list[asyncio.Task] = []
        self.last_received: float | None = None
        self.position_received = asyncio.Event()
        self.latest: dict[str, dict] = {}

    def start(self):
        for name, method in STREAMS.items():
            self.tasks.append(asyncio.create_task(self._pump(name, getattr(self.system.telemetry, method)())))

    async def _pump(self, name, stream):
        try:
            async for value in stream:
                sample = {"instance_id": self.instance_id, "type": name,
                          "received_at": datetime.now(timezone.utc).isoformat(),
                          "data": to_json(value)}
                self.last_received = asyncio.get_running_loop().time()
                self.latest[name] = sample["data"]
                if name == "position":
                    self.position_received.set()
                for queue in tuple(self.clients):
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    queue.put_nowait(sample)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def subscribe(self):
        queue = asyncio.Queue(maxsize=len(STREAMS))
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.clients.discard(queue)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
