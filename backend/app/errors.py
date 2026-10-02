from fastapi import Request
from fastapi.responses import JSONResponse


class BackendError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


async def error_response(_: Request, exc: BackendError):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message,
                                    "details": exc.details}}, status_code=exc.status)
