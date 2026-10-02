from fastapi import APIRouter, HTTPException, Request, WebSocket

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


@router.websocket("/api/v1/world/logs")
async def world_logs(websocket: WebSocket):
    from ..live_logs import serve_logs
    await serve_logs(websocket, websocket.app.state.runtime.process_manager.logs)
