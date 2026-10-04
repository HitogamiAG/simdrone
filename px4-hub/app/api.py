import asyncio
from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .errors import HubError, error_response
from .schemas import InstanceCreate, ParameterPatch

router = APIRouter()


@router.get("/api/v1/instances/{instance_id}/flight")
async def flight_state(instance_id: str, request: Request):
    return await request.app.state.service.flight_state(instance_id)


@router.post("/api/v1/instances/{instance_id}/flight/missions/validate")
async def flight_validate(instance_id: str, body: dict, request: Request):
    return await request.app.state.service.flight_validate(instance_id, body)


@router.post("/api/v1/instances/{instance_id}/flight/missions", status_code=202)
async def flight_start_mission(instance_id: str, body: dict, request: Request):
    result = await request.app.state.service.flight_start_mission(instance_id, body["mission"],
        body["request_id"])
    return {**result, "execution_id": result.get("execution_id")}


@router.get("/api/v1/instances/{instance_id}/flight/executions/{execution_id}")
async def flight_execution(instance_id: str, execution_id: str, request: Request):
    return await request.app.state.service.flight_execution(instance_id, execution_id)


@router.post("/api/v1/instances/{instance_id}/flight/executions/{execution_id}/cancel", status_code=202)
async def flight_cancel(instance_id: str, execution_id: str, body: dict, request: Request):
    return await request.app.state.service.flight_cancel(instance_id, execution_id, body.get("request_id"))


@router.post("/api/v1/instances/{instance_id}/flight/{action}", status_code=202)
async def flight_action(instance_id: str, action: str, body: dict, request: Request):
    if action not in {"return", "land"}: raise HubError(404, "route_not_found", "Unknown flight action")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise HubError(422, "invalid_request_id", "request_id is required")
    return await request.app.state.service.flight_action(instance_id, action, request_id)


@router.post("/api/v1/instances/{instance_id}/flight/offboard/sessions", status_code=201)
async def flight_create_session(instance_id: str, request: Request):
    return await request.app.state.service.flight_create_session(instance_id)


@router.get("/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}")
async def flight_session(instance_id: str, session_id: str, request: Request):
    return await request.app.state.service.flight_session(instance_id, session_id)


@router.post("/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}/{action}", status_code=202)
async def flight_session_action(instance_id: str, session_id: str, action: str, request: Request):
    if action not in {"arm", "disarm"}: raise HubError(404, "route_not_found", "Unknown session action")
    return await request.app.state.service.flight_session_action(instance_id, session_id, action)


@router.delete("/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}", status_code=202)
async def flight_delete_session(instance_id: str, session_id: str, request: Request):
    return await request.app.state.service.flight_delete_session(instance_id, session_id)


@router.websocket("/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}/control")
async def flight_control(instance_id: str, session_id: str, websocket: WebSocket):
    service = websocket.app.state.service
    try:
        flight = service._flight(instance_id)
        session = flight._session(session_id)
    except HubError:
        await websocket.close(code=1008, reason="offboard session unavailable")
        return
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), 5)
        token = first.get("token")
        if not isinstance(token, str) or not __import__("secrets").compare_digest(token, session.token):
            await websocket.close(code=1008, reason="invalid session token")
            return
        if session.owner_connected:
            await websocket.close(code=1008, reason="session already has a controller")
            return
        session.owner_connected = True
        while True:
            message = await websocket.receive_json()
            result = await flight.input(session_id, token, message)
            await websocket.send_json(result)
    except WebSocketDisconnect:
        pass
    except HubError as exc:
        await websocket.send_json({"type": "error", "code": exc.code, "message": exc.message})
        await websocket.close(code=1008, reason=exc.code)
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=1008, reason="invalid control handshake")
    finally:
        if 'session' in locals(): session.owner_connected = False


@router.websocket("/api/v1/instances/{instance_id}/flight/events")
async def flight_events(instance_id: str, websocket: WebSocket):
    service = websocket.app.state.service
    try:
        service._flight(instance_id)
    except HubError:
        await websocket.close(code=1008, reason="instance is not available")
        return
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"type": "flight", **service._flight(instance_id).state()})
            await asyncio.sleep(.25)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/api/v1/instances/")
async def list_instances(request: Request):
    return await request.app.state.service.list()


@router.post("/api/v1/instances/", status_code=201)
async def create_instance(body: InstanceCreate, request: Request):
    return await request.app.state.service.create(body.drone_id, body.profile)


@router.get("/api/v1/instances/{instance_id}/")
async def get_instance(instance_id: str, request: Request):
    return await request.app.state.service.get(instance_id)


@router.delete("/api/v1/instances/{instance_id}/")
async def delete_instance(instance_id: str, request: Request):
    return await request.app.state.service.delete(instance_id)


@router.post("/api/v1/instances/{instance_id}/restart")
async def restart_instance(instance_id: str, request: Request):
    return await request.app.state.service.restart(instance_id)


@router.post("/api/v1/instances/{instance_id}/stop")
async def stop_instance(instance_id: str, request: Request):
    return await request.app.state.service.stop(instance_id)


@router.post("/api/v1/instances/{instance_id}/start")
async def start_instance(instance_id: str, request: Request):
    return await request.app.state.service.start_instance(instance_id)


@router.get("/api/v1/instances/{instance_id}/parameters/")
async def get_parameters(instance_id: str, request: Request):
    return await request.app.state.service.get_parameters(instance_id)


@router.patch("/api/v1/instances/{instance_id}/parameters/")
async def patch_parameters(instance_id: str, body: ParameterPatch, request: Request):
    return await request.app.state.service.patch_parameters(instance_id, body.model_dump(exclude_unset=True))


@router.websocket("/api/v1/instances/{instance_id}/telemetry")
async def telemetry(instance_id: str, websocket: WebSocket):
    service = websocket.app.state.service
    try:
        record = service._get_running(instance_id)
    except HubError:
        await websocket.close(code=1008, reason="instance is not available")
        return
    fanout = record.telemetry
    if fanout is None:
        await websocket.close(code=1008, reason="instance telemetry is unavailable")
        return
    await websocket.accept()
    queue = fanout.subscribe()
    async def send_samples():
        while True:
            message = await queue.get()
            if message is None or record.telemetry is not fanout or fanout.closed:
                return
            await websocket.send_json(message)

    async def receive_disconnect():
        while True:
            if (await websocket.receive())["type"] == "websocket.disconnect":
                return

    async def generation_ended():
        while record.status == "running" and record.telemetry is fanout and not fanout.closed:
            await asyncio.sleep(0.2)

    generation = asyncio.create_task(generation_ended())
    tasks = [asyncio.create_task(send_samples()), asyncio.create_task(receive_disconnect()), generation]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            task.result()
        if generation in done or fanout.closed or record.telemetry is not fanout:
            await websocket.close(code=1012, reason="PX4 telemetry generation stopped")
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        fanout.unsubscribe(queue)


@router.websocket("/api/v1/instances/{instance_id}/logs")
async def instance_logs(instance_id: str, websocket: WebSocket):
    from .live_logs import serve_logs
    record = websocket.app.state.service.instances.get(instance_id)
    if record is None or record.logs is None or record.logs.closed:
        await websocket.close(code=1008, reason="instance logs are unavailable")
        return
    await serve_logs(websocket, record.logs)


def create_app(settings=None, service_factory=None):
    from contextlib import asynccontextmanager
    from .config import Settings
    from .service import InstanceService

    config = settings or Settings()
    factory = service_factory or InstanceService

    @asynccontextmanager
    async def lifespan(app):
        service = factory(config)
        app.state.service = service
        await service.start()
        try:
            yield
        finally:
            await service.shutdown()
            app.state.service = None

    app = FastAPI(title="PX4 Hub", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(HubError, error_response)

    async def validation_error(request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_input",
            "message": "Request validation failed", "details": exc.errors()}})

    app.add_exception_handler(RequestValidationError, validation_error)
    return app


app = create_app()
