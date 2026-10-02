class HubError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def error_response(request, exc: HubError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message, "details": exc.details
    }})
