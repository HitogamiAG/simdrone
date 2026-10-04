"""Opt-in acceptance tests that move the real Gazebo/PX4 vehicle."""
from __future__ import annotations

import math
import time
import uuid

import httpx
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
    # WebSocket upgrade completes before the Hub has necessarily claimed the
    # single-owner slot. Wait for the authoritative session state before arm.
    deadline = time.monotonic() + 5
    path = f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}"
    while time.monotonic() < deadline:
        response = httpx.get(BACKEND + path, timeout=2)
        response.raise_for_status()
        if response.json().get("owner_connected"):
            return ws
        time.sleep(.05)
    ws.close()
    raise AssertionError("Hub did not claim the Offboard controller WebSocket")


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


def yaw_from_pose(pose):
    q = pose["orientation"]
    return math.atan2(2 * (q["w"] * q["z"] + q["x"] * q["y"]),
                      1 - 2 * (q["y"] ** 2 + q["z"] ** 2))


def command_and_pose(flight, ws, seq, model, seconds, **axes):
    before = flight.gazebo.current(model)
    seq = drive(ws, seq, seconds, **axes)
    seq = drive(ws, seq, .6)
    after = flight.gazebo.current(model)
    return seq, before, after


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
            completed_route_in_rtl = (flight_mode in {"RETURN_TO_LAUNCH", "RTL"} and
                                      execution.get("current_waypoint", 0) >= len(waypoints))
            if now - mission_phase_at >= 12 and flight_mode != "MISSION" and not completed_route_in_rtl:
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


def test_real_offboard_body_axes_yaw_and_speed_limits(flight):
    created = flight.create_drone()
    drone_id, model = created["id"], created["name"]
    observer = flight.observers[-1]
    initial_alt = altitude(event_data(observer, "telemetry")[0])
    parameters = flight.request("PATCH", f"/api/v1/drones/{drone_id}/autopilot/parameters",
        expected=(200,), json={"MPC_XY_VEL_MAX": 1.2, "MPC_Z_VEL_MAX_UP": .6,
                               "MPC_Z_VEL_MAX_DN": .6}).json()
    assert parameters["parameters"]["MPC_XY_VEL_MAX"] == pytest.approx(1.2)
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    assert session["limits"]["horizontal_m_s"] == pytest.approx(1.2)
    assert session["limits"]["up_m_s"] <= 1
    assert session["limits"]["down_m_s"] <= 1
    assert session["limits"]["yaw_deg_s"] <= 60
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, 2.5)
        # With the PX4 ascent limit reduced to 0.6 m/s, allow enough time for
        # the vehicle to clear ground effect and require a physical climb.
        seq, _, raised = command_and_pose(flight, ws, seq, model, 6, up=.7)
        assert raised["position"]["z"] > initial_alt + 1.2
        vertical = observer.telemetry()["velocity"].get("down_m_s", 0)
        assert abs(float(vertical)) <= session["limits"]["up_m_s"] * 1.2
        seq, forward_start, forward_end = command_and_pose(flight, ws, seq, model, 1.5, forward=.45)
        seq, right_start, right_end = command_and_pose(flight, ws, seq, model, 1.5, right=.45)
        seq, yaw_start, turned = command_and_pose(flight, ws, seq, model, 2, yaw=.5)
        seq, reverse_start, reverse_end = command_and_pose(flight, ws, seq, model, 1.5, forward=-.45)
        seq, left_start, left_end = command_and_pose(flight, ws, seq, model, 1.5, right=-.45)
        seq, up_start, lowered = command_and_pose(flight, ws, seq, model, 1.5, up=-.5)

        def displacement(a, b):
            return (b["position"]["x"] - a["position"]["x"],
                    b["position"]["y"] - a["position"]["y"])

        fx, fy = displacement(forward_start, forward_end)
        # PX4 body-right maps to the negative local Y axis of Gazebo's FLU model.
        yaw0 = yaw_from_pose(forward_start)
        fproj = fx * math.cos(yaw0) + fy * math.sin(yaw0)
        assert fproj > .4, f"forward input moved opposite the body heading: {(fx, fy)}"
        rx, ry = displacement(right_start, right_end)
        right_proj = -rx * math.sin(yaw_from_pose(right_start)) + ry * math.cos(yaw_from_pose(right_start))
        assert right_proj < -.3, f"right input moved opposite body-right: {(rx, ry)}"
        yaw_delta = math.atan2(math.sin(yaw_from_pose(turned) - yaw_from_pose(yaw_start)),
                               math.cos(yaw_from_pose(turned) - yaw_from_pose(yaw_start)))
        assert abs(yaw_delta) > math.radians(10), f"yaw input did not rotate the vehicle: {yaw_delta}"
        bx, by = displacement(reverse_start, reverse_end)
        reverse_proj = bx * math.cos(yaw_from_pose(reverse_start)) + by * math.sin(yaw_from_pose(reverse_start))
        assert reverse_proj < -.3, f"negative forward input did not move backward: {(bx, by)}"
        lx, ly = displacement(left_start, left_end)
        left_proj = -lx * math.sin(yaw_from_pose(left_start)) + ly * math.cos(yaw_from_pose(left_start))
        assert left_proj > .3, f"negative right input did not move left: {(lx, ly)}"
        assert lowered["position"]["z"] < up_start["position"]["z"] - .3
        assert lowered["position"]["z"] > created["simulation"]["pose"]["position"]["z"] + 1

        # Observe the established speed after the command transition.
        seq = drive(ws, seq, 2, forward=.5, right=.5)
        telemetry = observer.telemetry()
        velocity = telemetry["velocity"]
        speed = math.hypot(float(velocity.get("north_m_s", 0)),
                           float(velocity.get("east_m_s", 0)))
        assert speed <= session["limits"]["horizontal_m_s"] * 1.2, \
            f"diagonal speed {speed:.2f}m/s exceeds the advertised limit"
    finally:
        ws.close()
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/return", expected=(202,),
                   json={"request_id": str(uuid.uuid4())})
    assert flight.wait_landed(drone_id,
        flight.native_geodetic(created["simulation"]["pose"]["position"]))["armed"] is False


def test_real_offboard_ownership_token_delete_and_current_location_land(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    session_path = f"/api/v1/drones/{drone_id}/flight/offboard/sessions"
    session = flight.request("POST", session_path, expected=(201,),
                             json={"request_id": str(uuid.uuid4())}).json()
    flight.request("POST", session_path, expected=(409,),
                   json={"request_id": str(uuid.uuid4())})
    conflict_mission = flight.mission(f"exclusive-{uuid.uuid4().hex[:8]}",
                                      [{"x": 16, "y": -8, "z": 6}])
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions", expected=(409,), json={
        "mission_id": conflict_mission["id"], "revision": conflict_mission["revision"],
        "request_id": str(uuid.uuid4())})
    ws = control_socket(drone_id, session)
    try:
        # An invalid handshake must not claim the controller slot.
        foreign = connect(BACKEND.replace("http://", "ws://") +
            f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/control",
            open_timeout=10, close_timeout=2, max_size=16384)
        foreign.send('{"token":"not-the-session-token"}')
        with pytest.raises(Exception):
            foreign.recv(timeout=2)
        foreign.close()
        # A second socket using the valid token is rejected while this owner holds it.
        duplicate = connect(BACKEND.replace("http://", "ws://") +
            f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/control",
            open_timeout=10, close_timeout=2, max_size=16384)
        duplicate.send(__import__("json").dumps({"token": session["token"]}))
        with pytest.raises(Exception):
            duplicate.recv(timeout=2)
        duplicate.close()
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        drive(ws, 0, 2.5)
    finally:
        ws.close()
    flight.request("DELETE", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}",
                   expected=(202,))

    # A fresh control owner drives a short climb, then DELETE must RTL and land.
    session = flight.request("POST", session_path, expected=(201,),
                             json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        initial = altitude(event_data(flight.observers[-1], "telemetry")[0])
        seq = drive(ws, 0, .5, up=.6)
        drive_to_altitude(ws, seq, flight.observers[-1], initial, {"up": .6}, target_delta=2)
    finally:
        ws.close()
    flight.request("DELETE", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}",
                   expected=(202,))
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    assert flight.wait_landed(drone_id, station)["armed"] is False

    # A subsequent session can use land to finish at the current location.
    session = flight.request("POST", session_path, expected=(201,),
                             json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        initial = altitude(event_data(flight.observers[-1], "telemetry")[0])
        seq = drive(ws, 0, .5, up=.6)
        drive_to_altitude(ws, seq, flight.observers[-1], initial, {"up": .6}, target_delta=2)
    finally:
        ws.close()
    landing_start = flight.gazebo.current(created["name"])
    landing_target = flight.native_geodetic(landing_start["position"])
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/land", expected=(202,),
                   json={"request_id": str(uuid.uuid4())})
    landed = flight.wait_landed(drone_id, landing_target)
    assert landed["armed"] is False
    landing_end = flight.gazebo.current(created["name"])
    assert math.dist((landing_start["position"]["x"], landing_start["position"]["y"], landing_start["position"]["z"]),
                     (landing_end["position"]["x"], landing_end["position"]["y"], landing_end["position"]["z"])) <= 3
    reopened = flight.request("POST", session_path, expected=(201,),
                              json={"request_id": str(uuid.uuid4())}).json()
    flight.request("DELETE", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{reopened['session_id']}",
                   expected=(202,))


def test_real_offboard_input_recovers_before_watchdog_rtl(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    initial = altitude(event_data(flight.observers[-1], "telemetry")[0])
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, .8, up=.6)
        seq, _ = drive_to_altitude(ws, seq, flight.observers[-1], initial, {"up": .6}, target_delta=2)
        time.sleep(2)
        seq = drive(ws, seq, 1, up=0)
        state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
        assert state.get("offboard", {}).get("status") == "active", state
        seq = drive(ws, seq, 1, up=.4)
        assert flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()["armed"] is True
    finally:
        ws.close()
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/return", expected=(202,),
                   json={"request_id": str(uuid.uuid4())})
    assert flight.wait_landed(drone_id,
        flight.native_geodetic(created["simulation"]["pose"]["position"]))["armed"] is False


def test_real_offboard_duplicate_sequence_does_not_extend_watchdog(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    initial = altitude(event_data(flight.observers[-1], "telemetry")[0])
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, .8, up=.6)
        seq, _ = drive_to_altitude(ws, seq, flight.observers[-1], initial, {"up": .6}, target_delta=2)
        duplicate = seq - 1
        ws.send(__import__("json").dumps({"seq": duplicate, "forward": .8, "right": 0,
                                          "up": 0, "yaw": 0}))
        error = __import__("json").loads(ws.recv(timeout=2))
        assert error.get("type") == "error" and error.get("code") == "invalid_offboard_input", error
        rejected_at = time.monotonic()
        with pytest.raises(Exception):
            ws.recv(timeout=2)
    finally:
        ws.close()

    # Rejected traffic does not refresh last_input: neutral setpoints and RTL
    # must still occur relative to the last accepted sequence.
    deadline = rejected_at + 3
    speed = float("inf")
    while time.monotonic() < deadline:
        telemetry = flight.observers[-1].telemetry()
        v = telemetry.get("velocity", {})
        speed = math.sqrt(sum(float(v.get(axis, 0)) ** 2 for axis in
                              ("north_m_s", "east_m_s", "down_m_s")))
        if speed < .3:
            break
        time.sleep(.15)
    assert speed < .3, f"duplicate input extended movement beyond watchdog: {speed:.2f}m/s"
    deadline = rejected_at + 7
    state = None
    while time.monotonic() < deadline:
        state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
        if state.get("offboard", {}).get("status") == "returning":
            break
        time.sleep(.15)
    assert state and state.get("offboard", {}).get("status") == "returning", state
    assert flight.wait_landed(drone_id,
        flight.native_geodetic(created["simulation"]["pose"]["position"]))["armed"] is False


def test_real_mission_autonomy_and_snapshot_survive_update_delete(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    waypoints = [{"x": 16, "y": -8, "z": 6}, {"x": 16, "y": -4, "z": 8},
                 {"x": 12, "y": -4, "z": 7}, {"x": 8, "y": -8, "z": 6}]
    mission = flight.mission(f"snapshot-{uuid.uuid4().hex[:8]}", waypoints)
    body = {"mission_id": mission["id"], "revision": mission["revision"],
            "request_id": str(uuid.uuid4())}
    accepted = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions",
                              expected=(202,), json=body).json()
    # A public flight-event viewer may leave; PX4 owns and continues execution.
    viewer = connect(BACKEND.replace("http://", "ws://") + "/api/v1/realtime",
                     open_timeout=10, close_timeout=2, max_size=16384)
    viewer.send(__import__("json").dumps({"action": "subscribe", "channels":
        [f"drone.{drone_id}.flight"]}))
    viewer.close()
    updated = flight.request("PUT", f"/api/v1/missions/{mission['id']}/", expected=(200,), json={
        "name": mission["name"], "world": mission["world"],
        "waypoints": [{"x": 12, "y": -8, "z": 6}], "cruise_speed_m_s": 1,
        "takeoff_height_m": 5, "return_height_m": 8, "expected_revision": mission["revision"]}).json()
    assert updated["revision"] == mission["revision"] + 1
    flight.request("DELETE", f"/api/v1/missions/{mission['id']}/", expected=(200,))
    flight.request("GET", f"/api/v1/missions/{mission['id']}/", expected=(404,))
    state, history = flight.wait_execution(drone_id, accepted["execution_id"], "completed",
                                           timeout=MISSION_TIMEOUT)
    assert state["status"] == "completed"
    observed = 0
    for waypoint in waypoints:
        # Use trajectory samples after acceptance to confirm the original route.
        for record in flight.gazebo.records:
            if record["name"] != created["name"]:
                continue
            p = record["position"]
            if math.hypot(p["x"] - waypoint["x"], p["y"] - waypoint["y"]) <= 2 and abs(p["z"] - waypoint["z"]) <= 1.5:
                observed += 1
                break
    assert observed == len(waypoints), f"active mission did not retain its original route ({observed}/4)"
    assert any(item["status"] == "active" for item in history)
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    assert flight.wait_landed(drone_id, station)["armed"] is False


def test_real_mission_rejects_stale_georeference_and_flies_changed_origin(flight):
    original = flight.request("GET", "/api/v1/world").json()
    old = flight.mission(f"old-origin-{uuid.uuid4().hex[:8]}", [{"x": 16, "y": -8, "z": 6}])
    changed = dict(original["spherical_coordinates"])
    changed.update({"latitude_deg": changed["latitude_deg"] + .0002,
                    "longitude_deg": changed["longitude_deg"] + .0002,
                    "elevation": changed["elevation"] + 25,
                    "heading_deg": 90.0})
    flight.request("PATCH", "/api/v1/world", expected=(200,), timeout=120,
                   json={"spherical_coordinates": changed})
    created = flight.create_drone(timeout=120)
    stale = flight.request("POST", f"/api/v1/missions/{old['id']}/validate", expected=(409,),
                           params={"drone_id": created["id"], "revision": old["revision"]})
    assert stale.json()["error"]["code"] == "mission_world_context_changed"
    points = [{"x": 16, "y": -8, "z": 6}, {"x": 16, "y": -4, "z": 8},
              {"x": 12, "y": -4, "z": 7}, {"x": 8, "y": -8, "z": 6}]
    mission = flight.mission(f"new-origin-{uuid.uuid4().hex[:8]}", points)
    validation = flight.request("POST", f"/api/v1/missions/{mission['id']}/validate", expected=(200,),
                                params={"drone_id": created["id"], "revision": mission["revision"]}).json()
    assert validation["valid"] is True
    accepted = flight.request("POST", f"/api/v1/drones/{created['id']}/flight/missions", expected=(202,),
        json={"mission_id": mission["id"], "revision": mission["revision"], "request_id": str(uuid.uuid4())}).json()
    state, _ = flight.wait_execution(created["id"], accepted["execution_id"], "completed", MISSION_TIMEOUT)
    assert state["status"] == "completed"
    pose_records = [r["position"] for r in flight.gazebo.records if r["name"] == created["name"]]
    for waypoint in points:
        assert any(math.hypot(p["x"] - waypoint["x"], p["y"] - waypoint["y"]) <= 2 and
                   abs(p["z"] - waypoint["z"]) <= 1.5 for p in pose_records), waypoint
    assert flight.wait_landed(created["id"], flight.native_geodetic(created["simulation"]["pose"]["position"]))["armed"] is False


def test_real_mission_can_be_cancelled_immediately_after_acceptance(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    mission = flight.mission(f"early-cancel-{uuid.uuid4().hex[:8]}", [
        {"x": 22, "y": -8, "z": 8}, {"x": 22, "y": 2, "z": 8},
        {"x": 2, "y": 2, "z": 8}, {"x": 2, "y": -18, "z": 8}])
    accepted = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions", expected=(202,), json={
        "mission_id": mission["id"], "revision": mission["revision"], "request_id": str(uuid.uuid4())}).json()
    execution_id = accepted["execution_id"]
    before = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/executions/{execution_id}").json()
    cancel = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/executions/{execution_id}/cancel",
                            expected=(202,), json={"request_id": str(uuid.uuid4())}).json()
    assert cancel["execution_id"] == execution_id
    assert before["status"] in {"preparing", "active"}, before
    state, _ = flight.wait_execution(drone_id, execution_id, "cancelled", RETURN_TIMEOUT)
    assert state["status"] == "cancelled"
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    assert flight.wait_landed(drone_id, station)["armed"] is False


def test_real_mission_and_offboard_are_independent_across_two_drones(flight):
    mission_drone = flight.create_drone(x=12, y=-8, z=1)
    offboard_drone = flight.create_drone(x=-12, y=12, z=1)
    points = [{"x": 16, "y": -8, "z": 6}, {"x": 16, "y": -4, "z": 8},
              {"x": 12, "y": -4, "z": 7}, {"x": 8, "y": -8, "z": 6}]
    mission = flight.mission(f"parallel-{uuid.uuid4().hex[:8]}", points)
    accepted = flight.request("POST", f"/api/v1/drones/{mission_drone['id']}/flight/missions",
        expected=(202,), json={"mission_id": mission["id"], "revision": mission["revision"],
                               "request_id": str(uuid.uuid4())}).json()

    session = flight.request("POST", f"/api/v1/drones/{offboard_drone['id']}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(offboard_drone["id"], session)
    try:
        flight.request("POST", f"/api/v1/drones/{offboard_drone['id']}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, .8, up=.6)
        drive_to_altitude(ws, seq, flight.observers[-1], 1, {"up": .6}, target_delta=2)
        mission_state = flight.request("GET", f"/api/v1/drones/{mission_drone['id']}/flight/executions/{accepted['execution_id']}").json()
        assert mission_state["status"] in {"preparing", "active", "returning", "completed"}
        flight.request("POST", f"/api/v1/drones/{offboard_drone['id']}/flight/return", expected=(202,),
                       json={"request_id": str(uuid.uuid4())})
    finally:
        ws.close()
    mission_state, _ = flight.wait_execution(mission_drone["id"], accepted["execution_id"],
                                              "completed", MISSION_TIMEOUT)
    assert mission_state["status"] == "completed"
    assert flight.wait_landed(mission_drone["id"],
        flight.native_geodetic(mission_drone["simulation"]["pose"]["position"]))["armed"] is False
    assert flight.wait_landed(offboard_drone["id"],
        flight.native_geodetic(offboard_drone["simulation"]["pose"]["position"]))["armed"] is False
