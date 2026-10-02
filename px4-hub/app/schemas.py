from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drone_id: str = Field(min_length=1, max_length=128)
    profile: Literal["x500_gimbal"] = "x500_gimbal"


class ParameterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    MPC_XY_VEL_MAX: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    MPC_Z_VEL_MAX_UP: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    MPC_Z_VEL_MAX_DN: float | None = Field(default=None, gt=0, allow_inf_nan=False)
