import asyncio
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from .errors import BackendError
from .schemas import DroneCreate, ParameterPatch, SensorPatch, WorldPatch

router = APIRouter()


@router.get("/healthz")
async def healthz(): return {"status": "ok"}


@router.get("/api/v1/healthz")
async def api_healthz(): return {"status": "ok"}


@router.get("/api/v1/system/status")
async def system_status(request: Request): return await request.app.state.platform.status()


@router.post("/api/v1/system/gazebo/reboot")
async def reboot(request: Request): return await request.app.state.platform.reset_world(reboot=True)


@router.get("/api/v1/world")
async def world(request: Request): return await request.app.state.platform.gazebo.world()


@router.patch("/api/v1/world")
async def patch_world(body: WorldPatch, request: Request):
    return await request.app.state.platform.patch_world(body.model_dump(exclude_unset=True))


@router.post("/api/v1/world/reset")
async def reset_world(request: Request): return await request.app.state.platform.reset_world()


@router.post("/api/v1/world/pause")
async def pause(request: Request): return await request.app.state.platform.set_paused(True)


@router.post("/api/v1/world/resume")
async def resume(request: Request): return await request.app.state.platform.set_paused(False)


@router.get("/api/v1/drones/")
async def drones(request: Request): return await request.app.state.platform.list_drones()


@router.post("/api/v1/drones/", status_code=201)
async def create_drone(body: DroneCreate, request: Request): return await request.app.state.platform.create_drone(body)


@router.get("/api/v1/drones/{drone_id}/")
async def drone(drone_id: str, request: Request): return await request.app.state.platform.get_drone(drone_id)


@router.delete("/api/v1/drones/{drone_id}/")
async def delete_drone(drone_id: str, request: Request): return await request.app.state.platform.delete_drone(drone_id)


@router.post("/api/v1/drones/{drone_id}/reset")
async def reset_drone(drone_id: str, request: Request): return await request.app.state.platform.reset_drone(drone_id)


@router.get("/api/v1/drones/{drone_id}/autopilot")
async def autopilot(drone_id: str, request: Request): return await request.app.state.platform.autopilot(drone_id)


def autopilot_action(action):
    async def run(drone_id: str, request: Request): return await request.app.state.platform.autopilot_action(drone_id, action)
    return run


router.add_api_route("/api/v1/drones/{drone_id}/autopilot/start", autopilot_action("start"), methods=["POST"])
router.add_api_route("/api/v1/drones/{drone_id}/autopilot/stop", autopilot_action("stop"), methods=["POST"])
router.add_api_route("/api/v1/drones/{drone_id}/autopilot/restart", autopilot_action("restart"), methods=["POST"])


@router.get("/api/v1/drones/{drone_id}/autopilot/parameters")
async def parameters(drone_id: str, request: Request): return await request.app.state.platform.parameters(drone_id)


@router.patch("/api/v1/drones/{drone_id}/autopilot/parameters")
async def patch_parameters(drone_id: str, body: ParameterPatch, request: Request):
    return await request.app.state.platform.patch_parameters(drone_id, body.model_dump(exclude_unset=True))


@router.get("/api/v1/drones/{drone_id}/sensors/")
async def sensors(drone_id: str, request: Request): return await request.app.state.platform.sensors(drone_id)


@router.get("/api/v1/drones/{drone_id}/sensors/{sensor_id}/")
async def sensor(drone_id: str, sensor_id: str, request: Request): return await request.app.state.platform.sensor(drone_id, sensor_id)


@router.patch("/api/v1/drones/{drone_id}/sensors/{sensor_id}/")
async def patch_sensor(drone_id: str, sensor_id: str, body: SensorPatch, request: Request):
    return await request.app.state.platform.patch_sensor(drone_id, sensor_id, body.model_dump())


@router.post("/api/v1/drones/{drone_id}/sensors/{sensor_id}/reset")
async def reset_sensor(drone_id: str, sensor_id: str, request: Request):
    return await request.app.state.platform.reset_sensor(drone_id, sensor_id)


@router.post("/api/v1/drones/{drone_id}/sensors/{sensor_id}/{action}")
async def camera_action(drone_id: str, sensor_id: str, action: str, request: Request):
    if action not in {"activate", "deactivate"}: raise BackendError(404, "route_not_found", "Unknown camera action")
    return await request.app.state.platform.sensor_action(drone_id, sensor_id, action)


@router.get("/api/v1/drones/{drone_id}/sensors/{sensor_id}/video")
async def video(drone_id: str, sensor_id: str, request: Request):
    return await request.app.state.platform.video(drone_id, sensor_id)


@router.websocket("/api/v1/realtime")
async def realtime_socket(ws: WebSocket):
    platform = ws.app.state.platform
    realtime = ws.app.state.realtime
    await ws.accept()
    subscriptions = {}
    incoming = asyncio.Queue(maxsize=8)
    async def receive_commands():
        try:
            while True:
                command = await ws.receive_json()
                if incoming.full():
                    incoming.get_nowait()
                incoming.put_nowait(command)
        except (WebSocketDisconnect, asyncio.CancelledError):
            while not incoming.empty():
                incoming.get_nowait()
            incoming.put_nowait({"action": "disconnect"})
    receiver = asyncio.create_task(receive_commands())
    try:
        while True:
            waiters = {asyncio.create_task(incoming.get()): None}
            for channel, queue in subscriptions.items():
                waiters[asyncio.create_task(queue.get())] = channel
            done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in pending: task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                channel = waiters[task]
                payload = task.result()
                if channel is None:
                    action, channels = payload.get("action"), payload.get("channels", [])
                    if action == "disconnect": return
                    if action == "subscribe":
                        if len(set(subscriptions) | set(channels)) > platform.settings.max_subscriptions:
                            await ws.send_json({"type": "error", "code": "subscription_limit"}); continue
                        for name in channels:
                            if name in subscriptions: continue
                            try:
                                subscriptions[name] = await realtime.subscribe(name)
                                await ws.send_json({"type": "subscribed", "channel": name})
                            except Exception as exc:
                                await ws.send_json({"type": "error", "channel": name, "message": str(exc)})
                    elif action == "unsubscribe":
                        for name in channels: await _drop(realtime, subscriptions, name)
                    else:
                        await ws.send_json({"type": "error", "code": "invalid_action"})
                elif payload.get("type") == "invalidated":
                    await ws.send_json(payload)
                    await _drop(realtime, subscriptions, channel)
                else:
                    await ws.send_json(payload)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        for channel in list(subscriptions): await _drop(realtime, subscriptions, channel)


async def _drop(realtime, subscriptions, channel):
    queue = subscriptions.pop(channel, None)
    if queue is not None: await realtime.unsubscribe(channel, queue)
