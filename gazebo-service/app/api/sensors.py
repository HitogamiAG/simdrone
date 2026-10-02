import asyncio
import logging
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from ..errors import ApiFault
from .schemas import SensorPatch

router = APIRouter()
log = logging.getLogger("gazebo-service")

@router.get("/api/v1/drones/{drone_id}/sensors/")
def list_sensors(drone_id: str, request: Request): return request.app.state.runtime.list_sensors(drone_id)

@router.get("/api/v1/drones/{drone_id}/sensors/{sensor_id}/")
def get_sensor(drone_id: str, sensor_id: str, request: Request): return request.app.state.runtime.get_sensor(drone_id, sensor_id)

@router.patch("/api/v1/drones/{drone_id}/sensors/{sensor_id}/")
def patch_sensor(drone_id: str, sensor_id: str, body: SensorPatch, request: Request): return request.app.state.runtime.patch_sensor(drone_id, sensor_id, body)

@router.post("/api/v1/drones/{drone_id}/sensors/{sensor_id}/reset")
def reset_sensor(drone_id: str, sensor_id: str, request: Request): return request.app.state.runtime.reset_sensor(drone_id, sensor_id)

@router.post("/api/v1/drones/{drone_id}/sensors/{sensor_id}/activate")
def activate_camera(drone_id: str, sensor_id: str, request: Request): return request.app.state.runtime.activate_camera(drone_id, sensor_id)

@router.post("/api/v1/drones/{drone_id}/sensors/{sensor_id}/deactivate")
def deactivate_camera(drone_id: str, sensor_id: str, request: Request): return request.app.state.runtime.deactivate_camera(drone_id, sensor_id)

@router.websocket("/api/v1/drones/{drone_id}/sensors/{sensor_id}/stream")
async def stream_sensor(ws: WebSocket, drone_id: str, sensor_id: str):
    await ws.accept()
    runtime = ws.app.state.runtime
    queue = asyncio.Queue(maxsize=1)
    generation = runtime.generation
    handle = None
    try:
        handle = await run_in_threadpool(runtime.open_stream, drone_id, sensor_id,
                                         asyncio.get_running_loop(), queue)
        if handle.is_camera:
            await ws.close(code=1008, reason="Camera video is available over RTSP")
            return
        if handle.latest:
            await ws.send_json(handle.latest)
        while True:
            if generation != runtime.generation or handle.retired:
                await ws.close(code=1012 if generation != runtime.generation else 1008, reason=handle.close_reason)
                break
            try: epoch, sample = await asyncio.wait_for(handle.queue.get(), timeout=.25)
            except asyncio.TimeoutError: continue
            if handle.retired or generation != runtime.generation or epoch != handle.epoch: continue
            await ws.send_json(sample)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except ApiFault as exc:
        await ws.close(code=1008, reason=exc.message)
    except Exception:
        log.exception("WebSocket sensor stream failed")
        try: await ws.close(code=1011, reason="sensor stream failed")
        except Exception: pass
    finally:
        if handle:
            try: await run_in_threadpool(handle.close)
            except Exception: pass
