from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import router
from .config import Settings
from .errors import BackendError, error_response
from .realtime import Realtime
from .service import Platform
from .flight import FlightPlatform


def create_app(settings=None, platform_factory=None):
    config = settings or Settings()
    factory = platform_factory or Platform

    @asynccontextmanager
    async def lifespan(app):
        platform = factory(config)
        app.state.platform = platform
        app.state.flight = FlightPlatform(platform)
        app.state.realtime = Realtime(platform)
        platform.realtime = app.state.realtime
        platform.startup_world_check = await platform.ready()
        try:
            yield
        finally:
            await app.state.realtime.close()
            app.state.flight.close()
            await platform.close()

    app = FastAPI(title="UAV Platform Backend", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(BackendError, error_response)

    async def validation_error(_, exc: RequestValidationError):
        return JSONResponse(
            {"error": {"code": "invalid_input", "message": "Request validation failed",
                        "details": jsonable_encoder(exc.errors())}},
            status_code=422,
        )

    app.add_exception_handler(RequestValidationError, validation_error)
    return app


app = create_app()

__all__ = ["app"]
