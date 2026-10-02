from fastapi import APIRouter, Request
from .schemas import DroneCreate

router = APIRouter()

@router.get("/api/v1/drones/")
def list_drones(request: Request): return request.app.state.runtime.list_drones()

@router.post("/api/v1/drones/", status_code=201)
def create_drone(body: DroneCreate, request: Request): return request.app.state.runtime.create_drone(body)

@router.get("/api/v1/drones/{drone_id}/")
def get_drone(drone_id: str, request: Request): return request.app.state.runtime.get_drone(drone_id)

@router.post("/api/v1/drones/{drone_id}/reset")
def reset_drone(drone_id: str, request: Request): return request.app.state.runtime.reset_drone(drone_id)

@router.delete("/api/v1/drones/{drone_id}/")
def delete_drone(drone_id: str, request: Request): return request.app.state.runtime.delete_drone(drone_id)
