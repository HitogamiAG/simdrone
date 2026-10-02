import math
from pydantic import BaseModel, ConfigDict, model_validator

class Vector3(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    z: float

    @model_validator(mode="after")
    def finite(self):
        if not all(math.isfinite(v) for v in (self.x, self.y, self.z)):
            raise ValueError("coordinates must be finite")
        return self

    def values(self):
        return (self.x, self.y, self.z)
class Quaternion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    z: float
    w: float

    @model_validator(mode="after")
    def normalize(self):
        values = (self.x, self.y, self.z, self.w)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("quaternion must be finite")
        norm = math.sqrt(sum(v * v for v in values))
        if norm < 1e-12:
            raise ValueError("quaternion must have non-zero magnitude")
        self.x, self.y, self.z, self.w = (v / norm for v in values)
        return self
class Pose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: Vector3
    orientation: Quaternion
