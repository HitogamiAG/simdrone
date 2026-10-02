import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    gazebo_api_url: str = os.getenv("GAZEBO_API_URL", "http://gazebo-service:8000")
    px4_binary: Path = Path(os.getenv("PX4_BINARY", "/opt/PX4-Autopilot/build/px4_sitl_default/bin/px4"))
    px4_rootfs: Path = Path(os.getenv("PX4_ROOTFS", "/opt/PX4-Autopilot/build/px4_sitl_default/rootfs"))
    instance_dir: Path = Path(os.getenv("INSTANCE_DIR", "/opt/uav/instances"))
    max_instances: int = min(3, max(1, int(os.getenv("MAX_INSTANCES", "3"))))
    startup_timeout: float = float(os.getenv("PX4_STARTUP_TIMEOUT", "90"))
    stop_timeout: float = float(os.getenv("PX4_STOP_TIMEOUT", "10"))
    poll_interval: float = 1.0
    telemetry_stale_after: float = 5.0
    partition: str = os.getenv("GZ_PARTITION", "uav-sim")
    mavsdk_server_binary: str = os.getenv("MAVSDK_SERVER_BINARY", "/opt/venv/lib/python3.12/site-packages/mavsdk_grpc/bin/mavsdk_server")
