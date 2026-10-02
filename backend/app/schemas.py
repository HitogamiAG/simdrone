import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Vector3(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    z: float

    @model_validator(mode="after")
    def finite(self):
        if not all(math.isfinite(v) for v in self.model_dump().values()):
            raise ValueError("coordinates must be finite")
        return self


class Quaternion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = 0
    y: float = 0
    z: float = 0
    w: float = 1

    @model_validator(mode="after")
    def normalized(self):
        values = list(self.model_dump().values())
        if not all(math.isfinite(v) for v in values):
            raise ValueError("quaternion must be finite")
        norm = math.sqrt(sum(v * v for v in values))
        if norm < 1e-12:
            raise ValueError("quaternion must be nonzero")
        self.x, self.y, self.z, self.w = (v / norm for v in values)
        return self


class Pose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: Vector3
    orientation: Quaternion = Field(default_factory=Quaternion)


class DroneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["x500_gimbal"] = "x500_gimbal"
    name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
    pose: Pose = Field(default_factory=lambda: Pose(position=Vector3(x=0, y=0, z=1)))


class ParameterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    MPC_XY_VEL_MAX: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    MPC_Z_VEL_MAX_UP: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    MPC_Z_VEL_MAX_DN: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_dump(exclude_unset=True):
            raise ValueError("at least one parameter is required")
        return self


class SensorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    update_rate: float = Field(ge=0, allow_inf_nan=False)


class WorldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    physics: dict[str, float] | None = None
    gravity: Vector3 | None = None
    spherical_coordinates: dict[str, float | str] | None = None

    @model_validator(mode="after")
    def supported(self):
        if not self.physics and self.gravity is None and self.spherical_coordinates is None:
            raise ValueError("at least one supported world setting is required")
        if set(self.physics or {}) - {"max_step_size", "real_time_factor"}:
            raise ValueError("unsupported physics field")
        for value in (self.physics or {}).values():
            if not math.isfinite(value) or value <= 0:
                raise ValueError("physics values must be finite and positive")
        allowed = {"latitude_deg", "longitude_deg", "elevation", "heading_deg", "surface_model"}
        if set(self.spherical_coordinates or {}) - allowed:
            raise ValueError("unsupported spherical coordinate field")
        for key, value in (self.spherical_coordinates or {}).items():
            if key == "surface_model":
                if value != "EARTH_WGS84": raise ValueError("only EARTH_WGS84 is supported")
            elif not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
        coords = self.spherical_coordinates or {}
        if not -90 <= coords.get("latitude_deg", 0) <= 90:
            raise ValueError("latitude_deg out of range")
        if not -180 <= coords.get("longitude_deg", 0) <= 180:
            raise ValueError("longitude_deg out of range")
        return self
