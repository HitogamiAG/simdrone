import os
from dataclasses import dataclass
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parents[1]
if LOCAL_ROOT.name == "gazebo-service":
    LOCAL_ROOT = LOCAL_ROOT.parent
ROOT = Path("/opt/uav")

@dataclass(frozen=True)
class Settings:
    world_path: Path = Path(os.getenv("WORLD_SDF", str(LOCAL_ROOT / "worlds/empty.sdf")))
    world_name: str = os.getenv("WORLD_NAME", "empty")
    model_roots: tuple[Path, ...] = tuple(Path(item) for item in os.getenv("MODEL_ROOTS", str(LOCAL_ROOT / "models")).split(":") if item)
    api_base: str = os.getenv("PUBLIC_API_URL", "http://gazebo-service:8000").rstrip("/")
    mediamtx_rtsp_host: str = os.getenv("MEDIAMTX_RTSP_HOST", "localhost")
    media_dir: Path = Path(os.getenv("MEDIA_DIR", "/tmp/gazebo-streams"))
    startup_timeout: float = float(os.getenv("STARTUP_TIMEOUT", "90"))
    request_timeout_ms: int = int(os.getenv("GZ_REQUEST_TIMEOUT_MS", "4000"))
    sensor_sample_timeout: float = float(os.getenv("SENSOR_SAMPLE_TIMEOUT", "5"))
    gazebo_log_file: str = os.getenv("GZ_LOG_FILE", "/tmp/gazebo-server.log")
    gazebo_verbosity: str = os.getenv("GZ_VERBOSITY", "2")
    render_engine: str = os.getenv("GZ_RENDER_ENGINE", "ogre2")
    rtsp_port: int = int(os.getenv("RTSP_PORT", "8554"))
    ffmpeg_preset: str = os.getenv("FFMPEG_PRESET", "ultrafast")
    mediamtx_api_user: str = os.getenv("MEDIAMTX_API_USER", "uav-api")
    mediamtx_api_password: str = os.getenv("MEDIAMTX_API_PASSWORD", "local-only-change-me")
    @classmethod
    def from_env(cls):
        return cls()
