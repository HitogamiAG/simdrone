import asyncio
from fastapi import APIRouter, Request, WebSocket
from .schemas import DroneCreate

router = APIRouter()

@router.get("/api/v1/drones/")
def list_drones(request: Request): return request.app.state.runtime.list_drones()

@router.post("/api/v1/drones/", status_code=201)
def create_drone(body: DroneCreate, request: Request): return request.app.state.runtime.create_drone(body)

@router.get("/api/v1/drones/{drone_id}/")
def get_drone(drone_id: str, request: Request): return request.app.state.runtime.get_drone(drone_id)

@router.post("/api/v1/drones/{drone_id}/reset")
def reset_drone(drone_id: str, request: Request): return request.app.state.runtime.reset_drone(drone_id)

@router.delete("/api/v1/drones/{drone_id}/")
def delete_drone(drone_id: str, request: Request): return request.app.state.runtime.delete_drone(drone_id)

@router.websocket("/api/v1/drones/{drone_id}/pose/stream")
async def drone_pose_stream(drone_id: str, websocket: WebSocket):
    runtime = websocket.app.state.runtime
    try:
        runtime.drone_pose_sample(drone_id)
    except Exception:
        await websocket.close(code=1008, reason="drone pose is unavailable")
        return
    await websocket.accept()
    try:
        while True:
            sample = runtime.drone_pose_sample(drone_id)
            await websocket.send_json(sample)
            await asyncio.sleep(.1)
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            await websocket.close(code=1012, reason="drone pose stream ended")
        except Exception:
            pass
