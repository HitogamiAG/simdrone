from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

@router.get("/healthz")
def healthz(request: Request):
    state = request.app.state.runtime.status()
    if not state["world_ready"]:
        raise HTTPException(503, "Gazebo is not ready")
    return {"status": "ok", **state}

@router.get("/api/v1/server/server-alive/")
def server_alive(request: Request): return request.app.state.runtime.status()

@router.post("/api/v1/server/server-reboot/")
def server_reboot(request: Request): return request.app.state.runtime.reboot()
