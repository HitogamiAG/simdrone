import asyncio
from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .errors import HubError, error_response
from .schemas import InstanceCreate, ParameterPatch

router = APIRouter()


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
