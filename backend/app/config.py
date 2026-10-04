import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    gazebo_url: str = os.getenv("GAZEBO_API_URL", "http://gazebo-service:8000")
    hub_url: str = os.getenv("PX4_HUB_API_URL", "http://px4-hub:8002")
    media_url: str = os.getenv("MEDIAMTX_WHEP_URL", "http://localhost:8889")
    mediamtx_api_url: str = os.getenv("MEDIAMTX_API_URL", "http://mediamtx:9997")
    mediamtx_user: str = os.getenv("MEDIAMTX_API_USER", "uav-api")
    mediamtx_password: str = os.getenv("MEDIAMTX_API_PASSWORD", "")
    request_timeout: float = float(os.getenv("BACKEND_REQUEST_TIMEOUT", "30"))
    startup_timeout: float = float(os.getenv("BACKEND_STARTUP_TIMEOUT", "120"))
    max_subscriptions: int = int(os.getenv("BACKEND_MAX_SUBSCRIPTIONS", "32"))
    mission_db: str = os.getenv("MISSION_DB", "/data/missions.sqlite3")
