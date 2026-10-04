import math
import sys
import types
import asyncio
import time
from types import SimpleNamespace

from app.flight import FlightController, _geo
from app.api import create_app


def test_local_coordinates_are_rotated_by_world_heading():
    origin = {"latitude_deg": 0.0, "longitude_deg": 0.0, "heading_deg": 90.0}
    lat, lon = _geo(origin, 10.0, 0.0)
    assert math.isclose(lat, -10.0 / 6378137 * 180 / math.pi, abs_tol=1e-9)
    assert math.isclose(lon, 0.0, abs_tol=1e-9)


def test_local_origin_maps_to_world_origin():
    origin = {"latitude_deg": 47.0, "longitude_deg": 8.0, "heading_deg": 37.0}
    assert _geo(origin, 0.0, 0.0) == (47.0, 8.0)


def test_offboard_velocity_is_limited_by_active_px4_and_resultant_speed(monkeypatch):
    offboard = types.ModuleType("mavsdk_grpc.offboard")
    offboard.VelocityBodyYawspeed = lambda forward, right, down, yaw: (forward, right, down, yaw)
    mavsdk = types.ModuleType("mavsdk_grpc")
    mavsdk.__path__ = []
    monkeypatch.setitem(sys.modules, "mavsdk_grpc", mavsdk)
    monkeypatch.setitem(sys.modules, "mavsdk_grpc.offboard", offboard)
    record = types.SimpleNamespace(saved_parameters={"MPC_XY_VEL_MAX": 2.0,
        "MPC_Z_VEL_MAX_UP": .7, "MPC_Z_VEL_MAX_DN": .8})
    controller = FlightController(None, record)
    forward, right, down, yaw = controller._limited_velocity(1, 1, 1, .5)
    assert math.isclose(math.hypot(forward, right), 2.0)
    assert down == -.7
    assert yaw == 30


def test_flight_routes_are_registered():
    paths = {route.path for route in create_app().routes}
    assert "/api/v1/instances/{instance_id}/flight" in paths
    assert "/api/v1/instances/{instance_id}/flight/missions" in paths
    assert "/api/v1/instances/{instance_id}/flight/offboard/sessions/{session_id}/control" in paths


def test_watchdog_holds_then_returns_after_control_input_is_lost(monkeypatch):
    async def scenario():
        offboard_module = types.ModuleType("mavsdk_grpc.offboard")
        offboard_module.VelocityBodyYawspeed = lambda forward, right, down, yaw: (forward, right, down, yaw)
        sdk = types.ModuleType("mavsdk_grpc")
        sdk.__path__ = []
        monkeypatch.setitem(sys.modules, "mavsdk_grpc", sdk)
        monkeypatch.setitem(sys.modules, "mavsdk_grpc.offboard", offboard_module)

        class FakeOffboard:
            def __init__(self): self.values = []; self.stopped = False
            async def set_velocity_body(self, value): self.values.append(value)
            async def stop(self): self.stopped = True

        class FakeAction:
            def __init__(self): self.rtl = 0
            async def return_to_launch(self): self.rtl += 1
            async def disarm(self): pass

        offboard, action = FakeOffboard(), FakeAction()
        record = SimpleNamespace(id="instance", status="running", binding_valid=True, saved_parameters={}, station_pose=None,
            telemetry=SimpleNamespace(connected=True, latest={"health": {"is_armable": True},
                "armed": False, "landed_state": "ON_GROUND", "position": {}},
                received_by_type={"position": asyncio.get_running_loop().time()}),
            system=SimpleNamespace(offboard=offboard, action=action))
        service = SimpleNamespace(simulation_paused=False,
            settings=SimpleNamespace(telemetry_stale_after=5.0),
            _get_running=lambda _instance_id: record)
        controller = FlightController(service, record)
        created = await controller.create_session()
        session = controller.session
        session.armed = True
        session.status = "active"
        session.last_input = time.monotonic() - 5.1
        await asyncio.sleep(.15)
        assert session.status == "returning"
        assert offboard.values[-1] == (0.0, 0.0, -0.0, 0.0)
        assert offboard.stopped and action.rtl == 1
        await controller.delete_session(created["session_id"])

    asyncio.run(scenario())


def test_return_remains_available_after_binding_and_armability_are_lost():
    async def scenario():
        class FakeAction:
            def __init__(self): self.rtl = 0
            async def return_to_launch(self): self.rtl += 1

        action = FakeAction()
        record = SimpleNamespace(id="instance", status="running", binding_valid=False,
            binding_error="model disappeared", saved_parameters={}, station_pose=None,
            telemetry=SimpleNamespace(connected=True, latest={"health": {"is_armable": False},
                "armed": True, "landed_state": "IN_AIR", "position": {}},
                received_by_type={"position": asyncio.get_running_loop().time()}),
            system=SimpleNamespace(action=action))
        service = SimpleNamespace(simulation_paused=False,
            settings=SimpleNamespace(telemetry_stale_after=5.0),
            _get_running=lambda _instance_id: record)
        controller = FlightController(service, record)
        result = await controller.action("return", "request-1")
        assert result["status"] == "returning"
        assert action.rtl == 1

    asyncio.run(scenario())
