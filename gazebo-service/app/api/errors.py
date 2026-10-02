import logging
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ..errors import ApiFault

log = logging.getLogger("gazebo-service")

async def api_fault_handler(request, exc: ApiFault):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})

async def validation_error_handler(request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_input", "message": "Request validation failed", "details": jsonable_encoder(exc.errors())}})

async def error_handler(request, exc: Exception):
    log.exception("Unhandled request error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Internal server error", "details": None}})
