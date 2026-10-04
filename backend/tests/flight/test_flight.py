"""Opt-in acceptance tests that move the real Gazebo/PX4 vehicle."""
from __future__ import annotations

import math
import time
import uuid

import pytest
from websockets.sync.client import connect

from conftest import (BACKEND, FlightEnvironment, MANEUVER_TIMEOUT,
                      MISSION_TIMEOUT, RETURN_TIMEOUT)


@pytest.fixture
def flight(request):
    env = FlightEnvironment()
    try:
        status = env.request("GET", "/api/v1/system/status").json()
        assert status["backend"]["ready"]
        assert env.request("GET", "/api/v1/drones/").json() == [], "isolated test registry must be empty"
        assert env.request("GET", "http://px4-hub:8002/api/v1/instances/").json() == []
        yield env
    finally:
        try:
            env.dump(request.node.name)
        except Exception:
            pass
        env.close()


def control_socket(drone_id, session):
    url = BACKEND.replace("http://", "ws://") + (
        f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/control")
    ws = connect(url, open_timeout=10, close_timeout=2, max_size=16384)
    ws.send(__import__("json").dumps({"token": session["token"]}))
    return ws


def send_axes(ws, seq, forward=0, right=0, up=0, yaw=0):
    import json
    ws.send(json.dumps({"seq": seq, "forward": forward, "right": right, "up": up, "yaw": yaw}))
    reply = json.loads(ws.recv(timeout=2))
    assert reply.get("seq") == seq, f"Offboard input was not accepted: {reply}"


def drive(ws, seq, seconds, **axes):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        send_axes(ws, seq, **axes)
        seq += 1
        time.sleep(max(0, .05 - .001))
    return seq


def drive_to_altitude(ws, seq, observer, initial_altitude, axes, target_delta=4.0):
    deadline = time.monotonic() + MANEUVER_TIMEOUT
    check_at = 0.0
    latest = None
    while time.monotonic() < deadline:
        send_axes(ws, seq, **axes)
        seq += 1
        now = time.monotonic()
        if now >= check_at:
            latest = observer.telemetry(timeout=1)
            current = altitude(latest)
            if current is not None and current >= initial_altitude + target_delta:
                return seq, latest
            check_at = now + .5
        time.sleep(.049)
    raise AssertionError(f"Vertical input did not reach {target_delta}m within {MANEUVER_TIMEOUT}s: {latest}")


def event_data(observer, suffix, timeout=3):
    if suffix == "telemetry":
        return observer.telemetry(timeout=timeout), {}
    event = observer.latest(suffix, timeout)
    return event.get("data", {}).get("data", {}), event


def altitude(data):
    position = data.get("position", {})
    return position.get("relative_altitude_m")


def test_real_offboard_vertical_motion_zero_hold_and_rtl(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    observer = flight.observers[-1]
    initial, initial_event = event_data(observer, "telemetry")
    initial_alt = altitude(initial)
    assert initial_alt is not None
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    assert flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()["armed"] is False

    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, 3.2)
        held, _ = event_data(observer, "telemetry")
        assert abs(altitude(held) - initial_alt) < .5, "arm plus neutral setpoints unexpectedly took off"

        seq, airborne = drive_to_altitude(ws, seq, observer, initial_alt, {"up": .65})
        assert altitude(airborne) > initial_alt + 4, f"vertical input did not produce takeoff: {airborne}"
        assert flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/disarm",
                              expected=(409,), json={"request_id": str(uuid.uuid4())}).status_code == 409

        seq = drive(ws, seq, 3, up=0)
        stable, _ = event_data(observer, "telemetry")
        horizontal = stable.get("velocity", {})
        speed = math.sqrt(sum(float(horizontal.get(k, 0)) ** 2 for k in
                              ("north_m_s", "east_m_s", "down_m_s")))
        assert speed < .3, f"zero input did not stop the drone: {horizontal}"
    finally:
        ws.close()

    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/return", expected=(202,),
                   json={"request_id": str(uuid.uuid4())})
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    state = flight.wait_landed(drone_id, station, RETURN_TIMEOUT)
    assert state["armed"] is False


def test_real_mission_four_waypoints_progress_return_and_replay(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    observer = flight.observers[-1]
    name = created["name"]
    waypoints = [
        {"x": 16, "y": -8, "z": 6},
        {"x": 16, "y": -4, "z": 8},
        {"x": 12, "y": -4, "z": 7},
        {"x": 8, "y": -8, "z": 6},
    ]
    mission = flight.mission(f"route-{uuid.uuid4().hex[:8]}", waypoints)
    validation = flight.request("POST", f"/api/v1/missions/{mission['id']}/validate",
                                expected=(200,), params={"drone_id": drone_id,
                                                        "revision": mission["revision"]}).json()
    assert validation["valid"] is True
    body = {"mission_id": mission["id"], "revision": mission["revision"], "request_id": str(uuid.uuid4())}
    accepted = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions",
                              expected=(202,), json=body).json()
    replay = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions",
                            expected=(202,), json=body).json()
    assert replay["execution_id"] == accepted["execution_id"]

    deadline = time.monotonic() + MISSION_TIMEOUT
    next_waypoint = 0
    history = []
    history_at = 0.0
    mission_phase_at = None
    while time.monotonic() < deadline:
        execution = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/executions/{accepted['execution_id']}").json()
        now = time.monotonic()
        if now - history_at >= 1:
            history.append(execution)
            history_at = now
        if execution.get("phase") == "mission":
            mission_phase_at = mission_phase_at or now
            with observer.condition:
                mode_event = observer.latest_by_type.get("flight_mode")
                flight_mode = mode_event.get("data", {}).get("data") if mode_event else None
            if now - mission_phase_at >= 12 and flight_mode != "MISSION":
                flight.dump("mission-mode-not-entered", {"execution": execution,
                            "flight_mode": flight_mode, "history": history})
                raise AssertionError(f"Mission remained outside PX4 MISSION mode: {flight_mode}")
        pose = flight.gazebo.current(name)
        p = pose["position"]
        if next_waypoint < len(waypoints):
            target = waypoints[next_waypoint]
            horizontal = math.hypot(p["x"] - target["x"], p["y"] - target["y"])
            vertical = abs(p["z"] - target["z"])
            if horizontal <= 2 and vertical <= 1.5:
                next_waypoint += 1
        if execution["status"] in {"completed", "failed", "interrupted"}:
            break
        time.sleep(.2)
    else:
        flight.dump("mission-timeout", {"history": history, "next_waypoint": next_waypoint})
        raise AssertionError("Mission exceeded 180s")
    assert execution["status"] == "completed", f"mission did not complete: {execution}"
    assert next_waypoint == len(waypoints), f"waypoints not observed in order: {next_waypoint}/{len(waypoints)}; {history[-1]}"
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    landed = flight.wait_landed(drone_id, station)
    assert landed["armed"] is False


def test_real_offboard_stale_input_stops_then_returns(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    observer = flight.observers[-1]
    initial = altitude(event_data(observer, "telemetry")[0])
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, .8, up=.6)
        seq, airborne = drive_to_altitude(ws, seq, observer, initial, {"up": .6}, target_delta=2.5)
        assert altitude(airborne) >= initial + 2.5

        # Keep the control socket open while withholding valid input. This proves
        # the Hub watchdog, rather than a WebSocket disconnect, removes motion.
        stopped_at = time.monotonic()
        deadline = stopped_at + 3
        speed = float("inf")
        while time.monotonic() < deadline:
            data = observer.telemetry(timeout=2)
            velocity = data.get("velocity", {})
            speed = math.sqrt(sum(float(velocity.get(k, 0)) ** 2 for k in
                                  ("north_m_s", "east_m_s", "down_m_s")))
            if speed < .3:
                break
            time.sleep(.15)
        assert speed < .3, f"stale input did not stop motion within 3s: {speed:.2f}m/s"
        assert time.monotonic() - stopped_at < 3

        # No input has been restored; the independent flight state must show
        # RTL after the 5s watchdog deadline.
        deadline = stopped_at + 7
        state = None
        while time.monotonic() < deadline:
            state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
            if state.get("offboard", {}).get("status") == "returning":
                break
            time.sleep(.15)
        assert state and state.get("offboard", {}).get("status") == "returning", \
            f"watchdog did not begin RTL in 5-6s: {state}"
    finally:
        ws.close()
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    landed = flight.wait_landed(drone_id, station)
    assert landed["armed"] is False


def test_real_mission_cancel_returns_and_is_idempotent(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    mission = flight.mission(f"cancel-{uuid.uuid4().hex[:8]}", [
        {"x": 22, "y": -8, "z": 8}, {"x": 22, "y": 2, "z": 8},
        {"x": 2, "y": 2, "z": 8}, {"x": 2, "y": -18, "z": 8},
    ])
    accepted = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions",
                              expected=(202,), json={"mission_id": mission["id"],
                                  "revision": mission["revision"], "request_id": str(uuid.uuid4())}).json()
    execution_id = accepted["execution_id"]
    deadline = time.monotonic() + 60
    airborne = False
    while time.monotonic() < deadline:
        current = flight.gazebo.current(created["name"])["position"]
        if current["z"] > created["simulation"]["pose"]["position"]["z"] + 2:
            airborne = True
            break
        state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/executions/{execution_id}").json()
        assert state["status"] not in {"failed", "interrupted"}, state
        time.sleep(.2)
    assert airborne, "mission did not physically take off before cancellation"
    cancel_request = str(uuid.uuid4())
    path = f"/api/v1/drones/{drone_id}/flight/executions/{execution_id}/cancel"
    first = flight.request("POST", path, expected=(202,),
                           json={"request_id": cancel_request}).json()
    second = flight.request("POST", path, expected=(202,),
                            json={"request_id": cancel_request}).json()
    assert first["execution_id"] == second["execution_id"] == execution_id
    assert first["status"] in {"returning", "cancelled"}
    if first["status"] == "returning":
        interim = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/executions/{execution_id}").json()
        assert interim["status"] != "cancelled", "cancelled reported before confirmed landing"
    state, _ = flight.wait_execution(drone_id, execution_id, "cancelled", timeout=RETURN_TIMEOUT)
    assert state["status"] == "cancelled"
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    assert flight.wait_landed(drone_id, station)["armed"] is False
