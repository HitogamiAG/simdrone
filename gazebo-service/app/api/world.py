from fastapi import APIRouter, Request
from .schemas import WorldPatch

router = APIRouter()

@router.get("/api/v1/world")
def get_world(request: Request): return request.app.state.runtime.world_info()

@router.get("/api/v1/world/spawn-pads")
def get_spawn_pads(request: Request): return request.app.state.runtime.spawn_pad_catalog()

@router.patch("/api/v1/world")
def patch_world(body: WorldPatch, request: Request): return request.app.state.runtime.patch_world(body)

@router.post("/api/v1/world/world-reset")
def reset_world(request: Request): return {"reset": True, **request.app.state.runtime.reset_world()}

@router.post("/api/v1/world/pause")
def pause_world(request: Request): return request.app.state.runtime.set_paused(True)

@router.post("/api/v1/world/resume")
def resume_world(request: Request): return request.app.state.runtime.set_paused(False)
