import math
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..geometry import Pose, Quaternion, Vector3

class DroneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "test_quad"
    name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
    pose: Pose = Field(default_factory=lambda: Pose(
        position=Vector3(x=0, y=0, z=1), orientation=Quaternion(x=0, y=0, z=0, w=1)
    ))
class SensorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    update_rate: float

    @model_validator(mode="after")
    def valid_rate(self):
        if not math.isfinite(self.update_rate) or self.update_rate < 0:
            raise ValueError("update_rate must be a finite non-negative number")
        return self
class WorldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    physics: dict[str, float] | None = None
    gravity: Vector3 | None = None
    spherical_coordinates: dict[str, Any] | None = None

    @model_validator(mode="after")
    def supported(self):
        unsupported = set(self.physics or {}) - {"max_step_size", "real_time_factor"}
        if unsupported:
            raise ValueError(f"unsupported physics fields: {sorted(unsupported)}")
        if self.physics and any(not math.isfinite(v) or v <= 0 for v in self.physics.values()):
            raise ValueError("physics values must be finite and positive")
        if self.spherical_coordinates:
            expected = {"latitude_deg", "longitude_deg", "elevation", "heading_deg", "surface_model"}
            extra = set(self.spherical_coordinates) - expected
            if extra:
                raise ValueError(f"unsupported spherical_coordinates: {sorted(extra)}")
            for key, value in self.spherical_coordinates.items():
                if key != "surface_model" and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise ValueError(f"{key} must be a finite number")
            lat = self.spherical_coordinates.get("latitude_deg", 0)
            lon = self.spherical_coordinates.get("longitude_deg", 0)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("latitude/longitude out of range")
            if self.spherical_coordinates.get("surface_model", "EARTH_WGS84") != "EARTH_WGS84":
                raise ValueError("only EARTH_WGS84 is supported")
        if not self.physics and self.gravity is None and self.spherical_coordinates is None:
            raise ValueError("at least one supported setting is required")
        return self
