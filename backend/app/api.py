import asyncio
import json
import os
from pathlib import Path
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from .errors import BackendError
from .schemas import (DroneCreate, ParameterPatch, SensorPatch, WorldPatch,
                      MissionCreate, MissionUpdate, MissionRun, FlightRequest)

router = APIRouter()


@router.get("/api/v1/world/map")
async def world_map():
    """Return the static map package identity, without serving its geometry."""
    manifest_path = Path(os.getenv("WORLD_MAP_MANIFEST", "/models/empty/manifest.json"))
    if not manifest_path.is_file():
        return {"available": False, "reason": "world_model_missing"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "reason": "world_model_manifest_invalid"}
    required = ("package_id", "version", "world_name", "coordinate_system", "model")
    if not isinstance(manifest, dict) or any(key not in manifest for key in required):
        return {"available": False, "reason": "world_model_manifest_invalid"}
    return {"available": True, **{key: manifest[key] for key in required},
            "bounds": manifest.get("bounds"),
            "gazebo_to_glb": manifest.get("gazebo_to_glb"),
            "control_points": manifest.get("control_points", [])}


@router.get("/api/v1/missions/")
async def list_missions(request: Request, world: str | None = None):
    return await asyncio.to_thread(request.app.state.flight.store.list, world)


@router.post("/api/v1/missions/", status_code=201)
async def create_mission(body: MissionCreate, request: Request):
    return await request.app.state.flight.create_mission(body.model_dump())


@router.get("/api/v1/missions/{mission_id}/")
async def get_mission(mission_id: str, request: Request):
    mission = await asyncio.to_thread(request.app.state.flight.store.get, mission_id)
    if mission is None: raise BackendError(404, "mission_not_found", "Mission was not found")
    return mission


@router.put("/api/v1/missions/{mission_id}/")
async def update_mission(mission_id: str, body: MissionUpdate, request: Request):
    return await request.app.state.flight.update_mission(mission_id, body.model_dump())


@router.delete("/api/v1/missions/{mission_id}/")
async def delete_mission(mission_id: str, request: Request):
    deleted = await asyncio.to_thread(request.app.state.flight.store.delete, mission_id)
    if not deleted: raise BackendError(404, "mission_not_found", "Mission was not found")
    return {"deleted": True, "id": mission_id}


@router.post("/api/v1/missions/{mission_id}/validate")
async def validate_mission(mission_id: str, request: Request, drone_id: str, revision: int | None = None):
    return await request.app.state.flight.validate_mission(mission_id, drone_id, revision)


@router.get("/api/v1/drones/{drone_id}/flight")
async def flight_status(drone_id: str, request: Request):
    return await request.app.state.flight.flight_status(drone_id)


@router.post("/api/v1/drones/{drone_id}/flight/missions", status_code=202)
async def flight_start_mission(drone_id: str, body: MissionRun, request: Request):
    return await request.app.state.flight.start_mission(drone_id, body.model_dump())


@router.get("/api/v1/drones/{drone_id}/flight/executions/{execution_id}")
async def flight_execution(drone_id: str, execution_id: str, request: Request):
    return await request.app.state.flight.execution(drone_id, execution_id)


@router.post("/api/v1/drones/{drone_id}/flight/executions/{execution_id}/cancel", status_code=202)
async def flight_cancel(drone_id: str, execution_id: str, body: FlightRequest, request: Request):
    async with request.app.state.platform.lock:
        return await request.app.state.flight.cancel(drone_id, execution_id, body.request_id)


@router.post("/api/v1/drones/{drone_id}/flight/{action}", status_code=202)
async def flight_action(drone_id: str, action: str, body: FlightRequest, request: Request):
    if action not in {"return", "land"}: raise BackendError(404, "route_not_found", "Unknown flight action")
    async with request.app.state.platform.lock:
        flight = request.app.state.flight
        fingerprint = ("flight_action", action)
        replay = flight.control_replay(drone_id, body.request_id, fingerprint)
        if replay is not None: return replay
        drone = request.app.state.platform._record(drone_id)
        if not drone.instance_id: raise BackendError(409, "autopilot_not_running", "Drone has no PX4 instance")
        state = await request.app.state.platform.hub.flight_state(drone.instance_id)
        command = body.model_dump() | {"expected_generation": state["generation"]}
        result = await request.app.state.platform.hub.flight_action(drone.instance_id, action, command)
        flight.remember_control(drone_id, body.request_id, fingerprint, result)
        return result


@router.post("/api/v1/drones/{drone_id}/flight/offboard/sessions", status_code=201)
async def offboard_create(drone_id: str, body: FlightRequest, request: Request):
    async with request.app.state.platform.lock:
        flight = request.app.state.flight
        fingerprint = ("offboard_create",)
        replay = flight.control_replay(drone_id, body.request_id, fingerprint)
        if replay is not None: return replay
        drone = request.app.state.platform._record(drone_id)
        if not drone.instance_id: raise BackendError(409, "autopilot_not_running", "Drone has no PX4 instance")
        state = await request.app.state.platform.hub.flight_state(drone.instance_id)
        result = await request.app.state.platform.hub.create_offboard(drone.instance_id, body.request_id, state["generation"])
        flight.remember_control(drone_id, body.request_id, fingerprint, result)
        return result


@router.get("/api/v1/drones/{drone_id}/flight/offboard/sessions/{session_id}")
async def offboard_get(drone_id: str, session_id: str, request: Request):
    drone = request.app.state.platform._record(drone_id)
    if not drone.instance_id: raise BackendError(409, "autopilot_not_running", "Drone has no PX4 instance")
    return await request.app.state.platform.hub.offboard_get(drone.instance_id, session_id)


@router.post("/api/v1/drones/{drone_id}/flight/offboard/sessions/{session_id}/{action}", status_code=202)
async def offboard_action(drone_id: str, session_id: str, action: str, body: FlightRequest, request: Request):
    if action not in {"arm", "disarm"}: raise BackendError(404, "route_not_found", "Unknown offboard action")
    async with request.app.state.platform.lock:
        flight = request.app.state.flight
        fingerprint = ("offboard_action", session_id, action)
        replay = flight.control_replay(drone_id, body.request_id, fingerprint)
        if replay is not None: return replay
        drone = request.app.state.platform._record(drone_id)
        if not drone.instance_id: raise BackendError(409, "autopilot_not_running", "Drone has no PX4 instance")
        state = await request.app.state.platform.hub.flight_state(drone.instance_id)
        result = await request.app.state.platform.hub.offboard_action(drone.instance_id, session_id, action,
            body.request_id, state["generation"])
        flight.remember_control(drone_id, body.request_id, fingerprint, result)
        return result


@router.delete("/api/v1/drones/{drone_id}/flight/offboard/sessions/{session_id}", status_code=202)
async def offboard_delete(drone_id: str, session_id: str, request: Request):
    async with request.app.state.platform.lock:
        drone = request.app.state.platform._record(drone_id)
        if not drone.instance_id: raise BackendError(409, "autopilot_not_running", "Drone has no PX4 instance")
        state = await request.app.state.platform.hub.flight_state(drone.instance_id)
        return await request.app.state.platform.hub.offboard_delete(drone.instance_id, session_id, state["generation"])


@router.websocket("/api/v1/drones/{drone_id}/flight/offboard/sessions/{session_id}/control")
async def offboard_control(drone_id: str, session_id: str, websocket: WebSocket):
    import json
    from websockets.asyncio.client import connect
    platform = websocket.app.state.platform
    drone = platform.drones.get(drone_id)
    if not drone or not drone.instance_id:
        await websocket.close(code=1008, reason="drone is unavailable")
        return
    try:
        await websocket.accept()
        async with connect(platform.hub.websocket_url(drone.instance_id, session_id),
                          open_timeout=5, close_timeout=2, max_size=16384) as upstream:
            first = await websocket.receive_json()
            await upstream.send(json.dumps(first))
            async def client_to_hub():
                while True:
                    message = await websocket.receive_json()
                    await upstream.send(json.dumps(message))
            async def hub_to_client():
                async for message in upstream:
                    await websocket.send_text(message)
            tasks = [asyncio.create_task(client_to_hub()), asyncio.create_task(hub_to_client())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending: task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done: task.result()
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        try: await websocket.close(code=1011, reason="flight control connection ended")
        except Exception: pass


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
                    if payload.get("type") == "gap":
                        payload = {**payload, "channel": channel}
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
