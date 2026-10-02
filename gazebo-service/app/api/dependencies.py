from fastapi import Request, WebSocket


def runtime(request: Request):
    return request.app.state.runtime


def websocket_runtime(websocket: WebSocket):
    return websocket.app.state.runtime
