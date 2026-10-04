import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .geometry import Pose

@dataclass
class DroneRecord:
    id: str
    name: str
    model: str
    path: Path | None
    pose: Pose
    initial_pose: Pose | None = None
    api_created: bool = False
    initial_sdf: str | None = None
    entity_id: int | None = None
    entity_name: str | None = None

    @property
    def gazebo_model(self) -> str:
        return self.entity_name or self.name
@dataclass
class SensorRecord:
    id: str
    name: str
    kind: str
    topic: str
    rate: float | None = None
    initial_rate: float | None = None
    latest: dict[str, Any] | None = None
    publisher_type: str | None = None
    is_camera: bool = False
    queues: dict[asyncio.Queue, asyncio.AbstractEventLoop] = field(default_factory=dict)
    observers: set = field(default_factory=set)
    guard: threading.RLock = field(default_factory=threading.RLock)
    retired: bool = False
    epoch: int = 0
    close_reason: str = "Sensor replaced"
    observed_rate: float | None = None
    last_stamp: float | None = None
    rate_confirmed: bool = False
