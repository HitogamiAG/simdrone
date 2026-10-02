"""FastAPI application factory and process lifecycle."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from .api import errors
from .api.server import router as server_router
from .api.world import router as world_router
from .api.drones import router as drones_router
from .api.sensors import router as sensors_router
from .config import Settings
from .errors import ApiFault
from .runtime import RuntimeCoordinator


def create_app(settings: Settings | None = None, runtime_factory=None) -> FastAPI:
    config = settings or Settings.from_env()
    factory = runtime_factory or RuntimeCoordinator

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = factory(config)
        app.state.runtime = runtime
        try:
            await run_in_threadpool(runtime.start)
            yield
        finally:
            await run_in_threadpool(runtime.shutdown)
            app.state.runtime = None

    application = FastAPI(title="Gazebo Server API", version="1.0.0", lifespan=lifespan)
    application.add_exception_handler(ApiFault, errors.api_fault_handler)
    application.add_exception_handler(Exception, errors.error_handler)
    from fastapi.exceptions import RequestValidationError
    application.add_exception_handler(RequestValidationError, errors.validation_error_handler)
    for router in (server_router, world_router, drones_router, sensors_router):
        application.include_router(router)
    return application


app = create_app()
