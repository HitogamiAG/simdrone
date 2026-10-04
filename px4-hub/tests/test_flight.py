import math
import sys
import types
import asyncio
import time
from types import SimpleNamespace

from app.flight import Execution, FlightController, _geo, _mission_items, _mission_route_finished
from app.api import create_app
from app.adapters import Px4Adapter


def test_local_coordinates_are_rotated_by_world_heading():
    origin = {"latitude_deg": 0.0, "longitude_deg": 0.0, "heading_deg": 90.0}
    lat, lon, altitude = _geo(origin, 10.0, 0.0)
    # Matches Gazebo SphericalCoordinates::PositionTransform (LOCAL2 -> SPHERICAL).
    assert math.isclose(lat, 9.0436947704967e-05, abs_tol=1e-12)
    assert math.isclose(lon, 0.0, abs_tol=1e-12)
    assert math.isclose(altitude, 0.0, abs_tol=1e-4)


def test_local_origin_maps_to_world_origin():
    origin = {"latitude_deg": 47.0, "longitude_deg": 8.0, "heading_deg": 37.0}
    lat, lon, altitude = _geo(origin, 0.0, 0.0)
    assert math.isclose(lat, 47.0, abs_tol=1e-12)
    assert math.isclose(lon, 8.0, abs_tol=1e-12)
    assert math.isclose(altitude, 0.0, abs_tol=1e-4)


def test_missionraw_current_flag_selects_first_navigation_waypoint():
    items = _mission_items({"coordinate_context": {"latitude_deg": 43.0,
        "longitude_deg": 76.0, "elevation": 800.0, "heading_deg": 0.0},
        "cruise_speed_m_s": 2.0,
        "waypoints": [{"x": 1.0, "y": 2.0, "z": 6.0},
                      {"x": 3.0, "y": 4.0, "z": 7.0}]})
    assert [item.current for item in items] == [0, 1, 0, 0]
    assert items[1].command == 16


def test_missionraw_terminal_rtl_item_completes_route_after_all_waypoints():
    assert _mission_route_finished(4, 4, "RETURN_TO_LAUNCH")
    assert _mission_route_finished(4, 4, "RTL")
    assert not _mission_route_finished(3, 4, "RETURN_TO_LAUNCH")
    assert not _mission_route_finished(4, 4, "MISSION")


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
                received_by_type={name: asyncio.get_running_loop().time()
                                  for name in ("position", "health", "armed", "landed_state")}),
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


def test_watchdog_retries_rtl_until_telemetry_confirms_mode():
    async def scenario():
        controller, record, calls = _safety_fixture()
        await controller.create_session()
        session = controller.session
        session.armed = False
        session.status = "returning"
        record.telemetry.latest["flight_mode"] = "RETURN_TO_LAUNCH"
        session.rtl_last_requested = time.monotonic() - 1.1
        session.watchdog = asyncio.create_task(controller._watchdog(session))
        await asyncio.sleep(.15)
        assert calls.count("rtl") == 0
        record.telemetry.latest["flight_mode"] = "HOLD"
        session.rtl_last_requested = time.monotonic() - 1.1
        await asyncio.sleep(.15)
        assert calls.count("rtl") >= 1
        await controller.close()

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
        received_by_type={name: asyncio.get_running_loop().time() - 100
                          for name in ("position", "health", "armed", "landed_state")}),
            system=SimpleNamespace(action=action))
        service = SimpleNamespace(simulation_paused=False,
            settings=SimpleNamespace(telemetry_stale_after=5.0),
            _get_running=lambda _instance_id: record)
        controller = FlightController(service, record)
        result = await controller.action("return", "request-1")
        assert result["status"] == "returning"
        assert action.rtl == 1

    asyncio.run(scenario())


def test_mission_cannot_start_while_offboard_session_is_reserved():
    async def scenario():
        from app.errors import HubError

        controller = FlightController(SimpleNamespace(), SimpleNamespace())
        controller.session = object()
        controller.validate_mission = lambda _mission: None
        try:
            await controller.start_mission({"id": "m", "revision": 1}, "r")
        except HubError as error:
            assert error.code == "flight_mode_conflict"
        else:
            raise AssertionError("Mission accepted while Offboard was reserved")

    asyncio.run(scenario())


def test_stale_flight_generation_is_rejected():
    async def scenario():
        from app.errors import HubError

        controller = FlightController(SimpleNamespace(), SimpleNamespace())
        try:
            await controller.start_mission({}, "r", "old-generation")
        except HubError as error:
            assert error.code == "flight_generation_conflict"
        else:
            raise AssertionError("Command from an old flight generation was accepted")

    asyncio.run(scenario())


def test_offboard_loss_failsafe_parameters_are_written_and_read_back():
    class Parameters:
        values = {}
        async def set_param_float(self, name, value): self.values[name] = value
        async def set_param_int(self, name, value): self.values[name] = value
        async def get_param_float(self, name): return self.values[name]
        async def get_param_int(self, name): return self.values[name]

    async def scenario():
        param = Parameters()
        await Px4Adapter(SimpleNamespace()).configure_offboard_failsafe(SimpleNamespace(
            system=SimpleNamespace(param=param)))
        assert param.values == {"COM_OF_LOSS_T": 1.0, "COM_OBL_RC_ACT": 3}

    asyncio.run(scenario())


def test_offboard_session_create_replay_returns_original_token():
    async def scenario():
        now = asyncio.get_running_loop().time()
        record = SimpleNamespace(id="i", status="running", binding_valid=True, binding_error=None,
            saved_parameters={}, station_pose=None, coordinate_context=None,
            telemetry=SimpleNamespace(connected=True, latest={"health": {"is_armable": True},
                "armed": False, "landed_state": "ON_GROUND", "position": {}},
                received_by_type={name: now for name in ("position", "health", "armed", "landed_state")}),
            system=SimpleNamespace())
        service = SimpleNamespace(simulation_paused=False,
            settings=SimpleNamespace(telemetry_stale_after=5), _get_running=lambda _: record)
        controller = FlightController(service, record)
        first = await controller.create_session("same-request")
        retry = await controller.create_session("same-request")
        assert retry == first
        await controller.close()

    asyncio.run(scenario())


def _safety_fixture(*, fail_start=False):
    from app.flight import FlightController

    calls = []
    now = asyncio.get_running_loop().time()
    latest = {"health": {"is_armable": True}, "armed": False,
              "landed_state": "ON_GROUND", "position": {}}

    class Action:
        async def arm(self): calls.append("arm")
        async def disarm(self): calls.append("disarm")
        async def land(self): calls.append("land")
        async def return_to_launch(self): calls.append("rtl")

    class Offboard:
        async def set_velocity_body(self, _): calls.append("setpoint")
        async def start(self):
            calls.append("offboard_start")
            if fail_start: raise RuntimeError("start rejected")
        async def stop(self): calls.append("offboard_stop")

    record = SimpleNamespace(id="i", status="running", binding_valid=True,
        binding_error=None, saved_parameters={}, station_pose=None,
        telemetry=SimpleNamespace(connected=True, latest=latest,
            received_by_type={name: now for name in ("position", "health", "armed", "landed_state")} ),
        system=SimpleNamespace(action=Action(), offboard=Offboard()))
    service = SimpleNamespace(simulation_paused=False,
        settings=SimpleNamespace(telemetry_stale_after=5), _get_running=lambda _: record)
    return FlightController(service, record), record, calls


def test_partial_arm_with_delayed_armed_telemetry_requests_rtl():
    async def scenario():
        controller, _record, calls = _safety_fixture(fail_start=True)
        session = await controller.create_session()
        controller.session.owner_connected = True
        try:
            await controller.session_action(session["session_id"], "arm")
        except RuntimeError:
            pass
        assert "rtl" in calls
        assert controller.session.status == "returning"
        await controller.close()
    asyncio.run(scenario())


def test_pause_during_offboard_preparation_prevents_arm(monkeypatch):
    async def scenario():
        controller, _record, calls = _safety_fixture()
        session = await controller.create_session()
        controller.session.owner_connected = True
        original_sleep = asyncio.sleep

        async def pause_during_prepare(delay):
            if delay == 1.1:
                controller.service.simulation_paused = True
                return await original_sleep(0)
            return await original_sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", pause_during_prepare)
        try:
            await controller.session_action(session["session_id"], "arm")
        except Exception:
            pass
        assert "arm" not in calls
        await controller.close()
    asyncio.run(scenario())


def test_pause_revokes_unarmed_offboard_session():
    async def scenario():
        controller, _record, _calls = _safety_fixture()
        await controller.create_session()
        controller.service.simulation_paused = True
        await asyncio.sleep(.1)
        assert controller.session is None
        await controller.close()
    asyncio.run(scenario())


def test_accepted_arm_is_returned_even_if_telemetry_still_says_disarmed():
    async def scenario():
        controller, _record, calls = _safety_fixture(fail_start=True)
        session = await controller.create_session()
        controller.session.owner_connected = True
        # Keep the current armed=False sample fresh; an accepted arm still requires RTL
        # when Offboard.start then fails, because this sample may precede PX4 state change.
        _record.telemetry.received_by_type["armed"] = asyncio.get_running_loop().time()
        try: await controller.session_action(session["session_id"], "arm")
        except RuntimeError: pass
        assert "rtl" in calls
        await controller.close()
    asyncio.run(scenario())


def test_land_cancels_inflight_mission_return_task():
    async def scenario():
        from app.flight import Execution

        controller, _record, calls = _safety_fixture()
        execution = Execution("e", "r", "mission", status="returning")
        controller.active = execution
        entered, release = asyncio.Event(), asyncio.Event()

        async def pending_return():
            entered.set()
            await release.wait()
            calls.append("rtl")

        execution.return_task = asyncio.create_task(pending_return())
        await entered.wait()
        await controller.action("land", "land-request")
        release.set()
        await asyncio.sleep(0)
        assert calls == ["land"]
        await controller.close()
    asyncio.run(scenario())


def test_watchdog_and_return_transition_are_serialized():
    async def scenario():
        controller, _record, _calls = _safety_fixture()
        created = await controller.create_session()
        session = controller.session
        session.armed, session.status = True, "active"
        session.last_input = time.monotonic() - 1
        entered, release = asyncio.Event(), asyncio.Event()
        original = controller._send_velocity

        async def delayed_neutral(*axes):
            entered.set()
            await release.wait()
            await original(*axes)

        controller._send_velocity = delayed_neutral
        await entered.wait()
        returning = asyncio.create_task(controller.action("return", "return-request"))
        await asyncio.sleep(.02)
        release.set()
        await returning
        await asyncio.sleep(.08)
        assert session.status == "returning"
        await controller.delete_session(created["session_id"])
    asyncio.run(scenario())
