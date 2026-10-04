"""Проверки контракта аудита. Exit 1 означает найденные нарушения, а не успех.

MAVSDK-команды, телеметрия и Gazebo подменены; тип скорости берётся из SDK.
Это не тест настоящего полёта. Запуск описан в ../flight-api-audit.md.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import create_app
from app.errors import HubError
from app.flight import Execution, FlightController


@asynccontextmanager
async def fixture(*, fail_start=False, fail_setpoint=False):
    calls, velocities = [], []
    geo = {"latitude_deg": 0.0, "longitude_deg": 0.0, "heading_deg": 0.0, "elevation": 0.0}
    latest = {"health": {"is_armable": True}, "armed": False, "landed_state": "ON_GROUND",
              "position": {"latitude_deg": 0.0, "longitude_deg": 0.0, "absolute_altitude_m": 0.0},
              "home": {"latitude_deg": 0.0, "longitude_deg": 0.0, "absolute_altitude_m": 0.0}}

    class Action:
        async def arm(self):
            calls.append("arm")
            latest["armed"] = True

        async def disarm(self):
            calls.append("disarm")
            latest["armed"] = False

        async def return_to_launch(self):
            calls.append("rtl")

        async def land(self):
            calls.append("land")

    class Offboard:
        async def set_velocity_body(self, value):
            calls.append("setpoint")
            velocities.append((value.forward_m_s, value.right_m_s, value.down_m_s, value.yawspeed_deg_s))
            if fail_setpoint:
                raise RuntimeError("setpoint unavailable")

        async def start(self):
            calls.append("offboard_start")
            if fail_start:
                raise RuntimeError("offboard start rejected")

        async def stop(self):
            calls.append("offboard_stop")

    class Gazebo:
        async def world(self):
            return {"name": "empty", "spherical_coordinates": geo}

    record = SimpleNamespace(id="instance", status="running", binding_valid=True, binding_error=None,
        world="empty", saved_parameters={}, station_pose={"position": {"x": 0.0, "y": 0.0, "z": 0.0}},
        telemetry=SimpleNamespace(connected=True, latest=latest,
            received_by_type={"position": asyncio.get_running_loop().time()}),
        system=SimpleNamespace(action=Action(), offboard=Offboard()))
    service = SimpleNamespace(simulation_paused=False, gazebo=Gazebo(),
        settings=SimpleNamespace(telemetry_stale_after=5), _get_running=lambda _: record)
    controller = FlightController(service, record)
    mission = {"id": "m", "revision": 1, "world": "empty", "coordinate_context": geo,
        "waypoints": [{"x": 1.0, "y": 0.0, "z": 5.0}], "cruise_speed_m_s": 1.0,
        "takeoff_height_m": 3.0, "return_height_m": 5.0}
    try:
        yield controller, calls, velocities, mission
    finally:
        await controller.close()


async def require_conflict(operation):
    try:
        result = await operation
    except HubError as exc:
        assert exc.status == 409, f"Expected 409, got {exc.status}: {exc.code}"
    else:
        raise AssertionError(f"Conflicting operation was accepted: {result}")


async def mission_conflicts_with_reserved_offboard():
    async with fixture() as (c, _, _, mission):
        await c.create_session()
        await require_conflict(c.start_mission(mission, "request"))


async def landing_requires_fresh_telemetry():
    async with fixture() as (c, _, _, _mission):
        c.active = Execution("e", "r", "mission")
        c.record.telemetry.received_by_type = {
            name: asyncio.get_running_loop().time() - 1000
            for name in ("position", "landed_state", "armed")}
        try:
            await asyncio.wait_for(c._wait_landed(c.active), .15)
        except asyncio.TimeoutError:
            return
        raise AssertionError("Landing accepted from stale telemetry")


async def landing_requires_station_proximity():
    async with fixture() as (c, _, _, _mission):
        c.active = Execution("e", "r", "mission")
        c.record.telemetry.latest["position"]["latitude_deg"] = 1.0
        try:
            await asyncio.wait_for(c._wait_landed(c.active), .15)
        except asyncio.TimeoutError:
            return
        raise AssertionError("Landing accepted approximately 111 km away from station")


async def partial_arm_failure_is_reconciled():
    async with fixture(fail_start=True) as (c, calls, _, _mission):
        session = await c.create_session()
        c.session.owner_connected = True
        try:
            await c.session_action(session["session_id"], "arm")
        except (RuntimeError, HubError):
            pass
        await asyncio.sleep(.1)
        assert not c.telemetry()["armed"] or "rtl" in calls, (
            f"PX4 remains armed without recovery; session.armed={c.session.armed}, calls={calls}")


async def watchdog_failure_attempts_return():
    async with fixture(fail_setpoint=True) as (c, calls, _, _mission):
        await c.create_session()
        c.session.armed, c.session.status = True, "active"
        c.session.last_input = time.monotonic() - 1
        await asyncio.sleep(.15)
        assert "rtl" in calls, f"No RTL attempt after setpoint failure: status={c.session.status}, calls={calls}"


async def returning_session_cannot_rearm_in_air():
    async with fixture() as (c, _, _, _mission):
        session = await c.create_session()
        c.session.owner_connected = True
        c.session.armed, c.session.status = False, "returning"
        c.record.telemetry.latest.update(armed=True, landed_state="IN_AIR")
        await require_conflict(c.session_action(session["session_id"], "arm"))


async def pause_replaces_last_motion_setpoint():
    async with fixture() as (c, _, velocities, _mission):
        await c.create_session()
        await c._send_velocity(1, 0, 0, 0)
        c.session.armed, c.session.status = True, "active"
        c.service.simulation_paused = True
        await asyncio.sleep(.15)
        assert velocities[-1] == (0, 0, 0, 0), f"Last velocity remains nonzero during pause: {velocities[-1]}"


async def close_invalidates_inflight_validation():
    async with fixture() as (c, _, _, mission):
        entered, resume = asyncio.Event(), asyncio.Event()
        original_world = c.service.gazebo.world

        async def delayed_world():
            entered.set()
            await resume.wait()
            return await original_world()

        c.service.gazebo.world = delayed_world
        request = asyncio.create_task(c.start_mission(mission, "request"))
        await entered.wait()
        await c.close()
        resume.set()
        await require_conflict(request)


def rejected_websocket_preserves_owner():
    class StubFlight:
        def __init__(self):
            self.session = SimpleNamespace(token="token", owner_connected=False)

        def _session(self, _):
            return self.session

        async def input(self, *_):
            return {"type": "accepted", "seq": 1}

    class StubService:
        def __init__(self):
            self.flight = StubFlight()

        async def start(self):
            pass

        async def shutdown(self):
            pass

        def _flight(self, _):
            return self.flight

    service = StubService()
    with TestClient(create_app(service_factory=lambda _: service)) as client:
        path = "/api/v1/instances/i/flight/offboard/sessions/s/control"
        with client.websocket_connect(path) as first:
            first.send_json({"token": "token"})
            first.send_json({"seq": 1})
            first.receive_json()
            assert service.flight.session.owner_connected
            with client.websocket_connect(path) as rejected:
                rejected.send_json({"token": "wrong"})
                try:
                    rejected.receive_json()
                except WebSocketDisconnect:
                    pass
            assert service.flight.session.owner_connected, "Rejected connection released the actual owner"


async def main():
    failed = 0
    checks = (mission_conflicts_with_reserved_offboard, landing_requires_fresh_telemetry,
        landing_requires_station_proximity, partial_arm_failure_is_reconciled,
        watchdog_failure_attempts_return, returning_session_cannot_rearm_in_air,
        pause_replaces_last_motion_setpoint, close_invalidates_inflight_validation)
    for check in checks:
        try:
            await check()
        except Exception as exc:
            failed += 1
            print(json.dumps({"check": check.__name__, "passed": False, "error": str(exc)}), flush=True)
        else:
            print(json.dumps({"check": check.__name__, "passed": True}), flush=True)
    try:
        await asyncio.to_thread(rejected_websocket_preserves_owner)
    except Exception as exc:
        failed += 1
        print(json.dumps({"check": "rejected_websocket_preserves_owner", "passed": False, "error": str(exc)}), flush=True)
    else:
        print(json.dumps({"check": "rejected_websocket_preserves_owner", "passed": True}), flush=True)
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
