"""Opt-in acceptance tests that move the real Gazebo/PX4 vehicle."""
from __future__ import annotations

import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from websockets.sync.client import connect

from conftest import (BACKEND, HUB, FlightEnvironment, MANEUVER_TIMEOUT,
                      MISSION_TIMEOUT, RETURN_TIMEOUT)


def test_px4_log_filter_scans_only_a_bounded_tail(tmp_path):
    source = tmp_path / "px4.log"
    target = tmp_path / "filtered.log"
    source.write_bytes(b"noise\n" * 2_000_000 + b"INFO [tail] final-message\n")

    written = FlightEnvironment._filter_px4_log(
        source, target, max_bytes=1024, max_scan_bytes=4096)

    result = target.read_bytes()
    assert b"INFO [tail] final-message" in result
    assert b"scanned bytes" in result
    assert written == len(result)


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


def hub_flight_state(instance_id):
    response = httpx.get(f"{HUB}/api/v1/instances/{instance_id}/flight", timeout=5)
    response.raise_for_status()
    return response.json()


def hub_execution(instance_id, execution_id):
    response = httpx.get(
        f"{HUB}/api/v1/instances/{instance_id}/flight/executions/{execution_id}", timeout=5)
    response.raise_for_status()
    return response.json()


def pause_world(flight, paused):
    path = "/api/v1/world/pause" if paused else "/api/v1/world/resume"
    response = flight.request("POST", path, expected=(200,))
    assert response.json()["paused"] is paused


def wait_px4_offboard_failsafe_ulog(instance_id, timeout=20):
    from pyulog import ULog

    root = Path(os.getenv("PX4_INSTANCE_ARTIFACTS", "/px4-instances")) / instance_id
    required_messages = ("Failsafe activated", "RTL: start return", "RTL: land at destination",
                         "Landing detected", "Disarmed by landing")
    deadline = time.monotonic() + timeout
    latest = []
    while time.monotonic() < deadline:
        for path in root.rglob("*.ulg"):
            try:
                log = ULog(str(path))
                latest = [entry.message for entry in log.logged_messages]
                topics = {entry.name: entry.data for entry in log.data_list}
                landed = topics.get("vehicle_land_detected", {}).get("landed", [])
                arming = topics.get("vehicle_status", {}).get("arming_state", [])
                if (all(any(required in message for message in latest) for required in required_messages)
                        and len(landed) and landed[-1] == 1 and len(arming) and arming[-1] == 1):
                    return {"ulog": str(path), "messages": latest[-len(required_messages):],
                            "landed": int(landed[-1]), "arming_state": int(arming[-1])}
            except Exception:
                # PX4 can still be appending the current ULog; retry until the
                # diagnostic stream is readable or the bounded deadline expires.
                continue
        time.sleep(.5)
    raise AssertionError(f"PX4 ULog did not confirm Offboard-loss RTL/landing/disarm: {latest[-20:]}")


def wait_hub_landed_at_station(flight, instance_id, observer, model, station, timeout=RETURN_TIMEOUT):
    deadline = time.monotonic() + timeout
    stable_since = None
    latest = None
    while time.monotonic() < deadline:
        state = hub_flight_state(instance_id)
        latest = observer.telemetry()
        velocity = latest.get("velocity", {})
        speed = math.sqrt(sum(float(velocity.get(axis, 0)) ** 2 for axis in
                              ("north_m_s", "east_m_s", "down_m_s")))
        pose = flight.gazebo.current(model)
        horizontal = math.hypot(pose["position"]["x"] - station["x"],
                                pose["position"]["y"] - station["y"])
        on_ground = str(state.get("landed_state", "")).upper() in {"ON_GROUND", "LANDED"}
        if state.get("armed") is False and on_ground and speed < .2 and horizontal <= 3:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 2:
                return state
        else:
            stable_since = None
        time.sleep(.2)
    raise AssertionError(f"Hub/Gazebo did not confirm landing at station: {latest}; {state if 'state' in locals() else None}")


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


def test_real_spawn_pad_contact_on_both_pads_and_release_after_delete(flight):
    catalog = flight.request("GET", "/api/v1/world/spawn-pads").json()["pads"]
    pads = {pad["id"]: pad for pad in catalog}
    assert {"landing_pad_01", "landing_pad_02"} <= pads.keys()

    for pad_id in ("landing_pad_01", "landing_pad_02"):
        pad = pads[pad_id]
        created = flight.create_drone(pad_id=pad_id)
        drone_id, model = created["id"], created["name"]
        assert created["spawn_pad_id"] == pad_id
        assert created["initial_pose"] == pad["spawn_pose"]

        center = pad["surface_pose"]["position"]
        # The model's lowest gear collision is 13 mm below its root. Gazebo
        # should settle the 5 mm SDF clearance onto this actual surface.
        contact_root_z = center["z"] - .013
        deadline = time.monotonic() + 30
        stable_since = None
        latest = None
        while time.monotonic() < deadline:
            latest = flight.gazebo.current(model)
            state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
            landed = state.get("landed_state")
            landed_value = landed.get("landed_state") if isinstance(landed, dict) else landed
            position = latest["position"]
            in_contact_pose = (
                math.hypot(position["x"] - center["x"], position["y"] - center["y"]) < .05
                and abs(position["z"] - contact_root_z) < .025
            )
            px4_on_ground = state.get("armed") is False and str(landed_value).lower() in {"on_ground", "2"}
            if in_contact_pose and px4_on_ground:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 1:
                    break
            else:
                stable_since = None
            time.sleep(.2)
        else:
            raise AssertionError(
                f"{pad_id}: no stable chassis contact confirmed by Gazebo and PX4; "
                f"pose={latest}; flight={state}"
            )

        deleted = flight.request("DELETE", f"/api/v1/drones/{drone_id}/", expected=(200, 204))
        assert deleted.status_code in {200, 204}
        available = flight.request("GET", "/api/v1/world/spawn-pads").json()["pads"]
        assert next(item for item in available if item["id"] == pad_id)["availability"] == "available"


def test_real_offboard_pause_resume_returns_and_lands(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    observer = flight.observers[-1]
    station = created["simulation"]["pose"]["position"]
    initial, _ = event_data(observer, "telemetry")
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, 1.5)
        seq, airborne = drive_to_altitude(ws, seq, observer, altitude(initial), {"up": .65}, target_delta=3)
        assert altitude(airborne) >= altitude(initial) + 3
        drive(ws, seq, 1.0, forward=.35)
        pause_world(flight, True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}",
                                    expected=(200, 404)).status_code
            if status == 200:
                state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}").json()
                if state.get("status") == "paused":
                    break
            time.sleep(.2)
        else:
            raise AssertionError("Offboard session did not enter paused state")
    finally:
        ws.close()
        pause_world(flight, False)
    flight.wait_landed(drone_id, flight.native_geodetic(station), RETURN_TIMEOUT)
    landed = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
    assert landed["armed"] is False


def test_real_offboard_unarmed_session_is_revoked_by_pause(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    try:
        pause_world(flight, True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            response = httpx.get(BACKEND + f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}", timeout=3)
            if response.status_code == 404:
                break
            time.sleep(.2)
        else:
            raise AssertionError("Unarmed Offboard reservation survived world pause")
    finally:
        pause_world(flight, False)
    state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
    assert state["armed"] is False


def test_real_mavsdk_loss_preserves_px4_offboard_failsafe(flight):
    created = flight.create_drone()
    drone_id, model = created["id"], created["name"]
    station_xyz = created["simulation"]["pose"]["position"]
    initial_pose = flight.gazebo.current(model)
    instance_id = flight.instance_ids[drone_id]
    observer = flight.observers[-1]
    initial, _ = event_data(observer, "telemetry")
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                   expected=(202,), json={"request_id": str(uuid.uuid4())})
    seq = drive(ws, 0, 1.5)
    seq, airborne = drive_to_altitude(ws, seq, observer, altitude(initial), {"up": .65}, target_delta=3)
    assert altitude(airborne) >= altitude(initial) + 3
    hub_state = httpx.get(f"{HUB}/api/v1/instances/{instance_id}/", timeout=5)
    hub_state.raise_for_status()
    pids = hub_state.json()["process"]
    assert pids["px4_pid"] and pids["mavsdk_pid"]
    ws.close()
    FlightEnvironment.request_mavsdk_kill(pids["mavsdk_pid"], pids["px4_pid"], "offboard-failsafe")

    deadline = time.monotonic() + RETURN_TIMEOUT
    landed = None
    while time.monotonic() < deadline:
        landed = flight.gazebo.current(model)
        p = landed["position"]
        horizontal = math.hypot(p["x"] - station_xyz["x"], p["y"] - station_xyz["y"])
        if horizontal <= 3 and abs(p["z"] - station_xyz["z"]) <= 1.5:
            # Require a stable physical pose for two seconds, with Gazebo still
            # advancing; this is independent of the now-dead MAVSDK telemetry.
            stable_since = time.monotonic()
            previous = (p["x"], p["y"], p["z"])
            while time.monotonic() - stable_since < 2:
                time.sleep(.2)
                current = flight.gazebo.current(model)
                q = current["position"]
                horizontal_now = math.hypot(q["x"] - station_xyz["x"], q["y"] - station_xyz["y"])
                if (math.dist((q["x"], q["y"], q["z"]), previous) >= .03
                        or horizontal_now > 3 or abs(q["z"] - station_xyz["z"]) > 1.5):
                    break
                previous = (q["x"], q["y"], q["z"])
            else:
                break
        time.sleep(.2)
    else:
        raise AssertionError(f"PX4 Offboard-loss failsafe did not return to station: {landed}")
    assert landed is not None
    assert flight.gazebo.current(model)["sim_time_s"] > initial_pose["sim_time_s"]
    wait_px4_offboard_failsafe_ulog(instance_id)
    FlightEnvironment.request_px4_probe(pids["px4_pid"], "after-failsafe")


def test_real_offboard_three_consecutive_flights_without_reset(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    observer = flight.observers[-1]
    instance_id = flight.instance_ids[drone_id]
    model = created["name"]
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    generation = flight.wait_ready(drone_id)["generation"]

    for flight_number in range(1, 4):
        landed_state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
        assert landed_state["generation"] == generation, "flight generation changed without a drone reset"
        assert landed_state["armed"] is False and str(landed_state["landed_state"]).lower() in {"on_ground", "2"}
        initial, _ = event_data(observer, "telemetry")
        initial_alt = altitude(initial)
        session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                                  expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
        ws = control_socket(drone_id, session)
        try:
            flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                           expected=(202,), json={"request_id": str(uuid.uuid4())})
            seq = drive(ws, 0, 1.5)
            held, _ = event_data(observer, "telemetry")
            assert abs(altitude(held) - initial_alt) < .5, \
                f"flight {flight_number}: neutral arm caused takeoff"
            seq, airborne = drive_to_altitude(ws, seq, observer, initial_alt, {"up": .65}, target_delta=4)
            assert altitude(airborne) >= initial_alt + 4, \
                f"flight {flight_number}: Offboard did not climb"
            drive(ws, seq, 2, up=0)
        finally:
            ws.close()

        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/return", expected=(202,),
                       json={"request_id": str(uuid.uuid4())})
        landed = flight.wait_landed(drone_id, station)
        assert landed["armed"] is False
        assert flight.instance_ids[drone_id] == instance_id, \
            f"flight {flight_number}: PX4 instance changed without a drone reset"


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


def test_real_mission_pause_resume_preserves_autonomous_execution(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    mission = flight.mission(f"pause-{uuid.uuid4().hex[:8]}", [
        {"x": 18, "y": -8, "z": 6},
        {"x": 18, "y": -3, "z": 7},
        {"x": 12, "y": -3, "z": 6},
    ])
    accepted = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions", expected=(202,), json={
        "mission_id": mission["id"], "revision": mission["revision"],
        "request_id": str(uuid.uuid4())}).json()
    deadline = time.monotonic() + MISSION_TIMEOUT
    while time.monotonic() < deadline:
        execution = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/executions/{accepted['execution_id']}").json()
        if execution.get("phase") == "mission" and execution.get("status") == "active":
            break
        time.sleep(.2)
    else:
        raise AssertionError(f"Mission did not enter autonomous route phase: {execution}")
    try:
        pause_world(flight, True)
        time.sleep(2)
        paused = flight.request("GET", f"/api/v1/drones/{drone_id}/flight/executions/{accepted['execution_id']}").json()
        assert paused["status"] == "active" and paused["phase"] == "mission", \
            f"World pause interrupted autonomous mission: {paused}"
    finally:
        pause_world(flight, False)
    completed, _ = flight.wait_execution(drone_id, accepted["execution_id"], "completed", MISSION_TIMEOUT)
    assert completed["status"] == "completed"
    landed = flight.wait_landed(drone_id, station, RETURN_TIMEOUT)
    assert landed["armed"] is False


def test_real_mission_three_consecutive_flights_without_reset(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    instance_id = flight.instance_ids[drone_id]
    model = created["name"]
    observer = flight.observers[-1]
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    waypoints = [
        {"x": 16, "y": -8, "z": 6},
        {"x": 16, "y": -4, "z": 8},
        {"x": 12, "y": -4, "z": 7},
        {"x": 8, "y": -8, "z": 6},
    ]
    mission = flight.mission(f"repeat-{uuid.uuid4().hex[:8]}", waypoints)
    generation = flight.wait_ready(drone_id)["generation"]

    for flight_number in range(1, 4):
        landed_state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
        assert landed_state["generation"] == generation, "flight generation changed without a drone reset"
        assert landed_state["armed"] is False and str(landed_state["landed_state"]).lower() in {"on_ground", "2"}
        pose_start = len(flight.gazebo.records)
        accepted = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions", expected=(202,), json={
            "mission_id": mission["id"], "revision": mission["revision"],
            "request_id": str(uuid.uuid4())}).json()
        execution, _ = flight.wait_execution(drone_id, accepted["execution_id"], "completed", MISSION_TIMEOUT)
        assert execution["status"] == "completed"

        reached = 0
        poses = [sample["position"] for sample in flight.gazebo.records[pose_start:]
                 if sample["name"] == model]
        for pose in poses:
            target = waypoints[reached]
            if (math.hypot(pose["x"] - target["x"], pose["y"] - target["y"]) <= 2
                    and abs(pose["z"] - target["z"]) <= 1.5):
                reached += 1
                if reached == len(waypoints):
                    break
        assert reached == len(waypoints), \
            f"flight {flight_number}: only {reached}/{len(waypoints)} waypoints observed in order"
        landed = flight.wait_landed(drone_id, station)
        assert landed["armed"] is False
        assert flight.instance_ids[drone_id] == instance_id, \
            f"flight {flight_number}: PX4 instance changed without a drone reset"


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


def test_real_offboard_disconnect_stops_then_returns(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    observer = flight.observers[-1]
    initial = altitude(event_data(observer, "telemetry")[0])
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                   expected=(202,), json={"request_id": str(uuid.uuid4())})
    seq = drive(ws, 0, .8, up=.6)
    seq, airborne = drive_to_altitude(ws, seq, observer, initial, {"up": .6}, target_delta=2.5)
    assert altitude(airborne) >= initial + 2.5

    # Simulate the phone disappearing by abruptly closing its control socket.
    disconnected_at = time.monotonic()
    ws.close()
    deadline = disconnected_at + 3
    speed = float("inf")
    while time.monotonic() < deadline:
        velocity = observer.telemetry(timeout=2).get("velocity", {})
        speed = math.sqrt(sum(float(velocity.get(axis, 0)) ** 2 for axis in
                              ("north_m_s", "east_m_s", "down_m_s")))
        if speed < .3:
            break
        time.sleep(.15)
    assert speed < .3, f"disconnect did not stop motion within 3s: {speed:.2f}m/s"

    deadline = disconnected_at + 7
    state = None
    while time.monotonic() < deadline:
        state = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()
        if state.get("offboard", {}).get("status") == "returning":
            break
        time.sleep(.15)
    assert state and state.get("offboard", {}).get("status") == "returning", \
        f"socket loss did not start RTL within 5-6s: {state}"
    assert flight.wait_landed(drone_id,
        flight.native_geodetic(created["simulation"]["pose"]["position"]))["armed"] is False


def test_real_offboard_returns_after_backend_loss(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    instance_id = flight.instance_ids[drone_id]
    hub_observer = flight.hub_telemetry(instance_id)
    initial = altitude(event_data(flight.observers[-1], "telemetry")[0])
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                   expected=(202,), json={"request_id": str(uuid.uuid4())})
    seq = drive(ws, 0, .8, up=.6)
    seq, airborne = drive_to_altitude(ws, seq, flight.observers[-1], initial,
                                      {"up": .6}, target_delta=2.5)
    assert altitude(airborne) >= initial + 2.5

    # The host runner stops Backend while PX4 Hub and Gazebo remain available.
    FlightEnvironment.request_backend_restart("offboard")
    with pytest.raises(Exception):
        ws.recv(timeout=2)
    ws.close()
    assert flight.request("GET", "/api/v1/drones/").json() == [], \
        "Backend restart unexpectedly restored its in-memory public drone registry"

    deadline = time.monotonic() + 10
    state = None
    while time.monotonic() < deadline:
        state = hub_flight_state(instance_id)
        if state.get("offboard", {}).get("status") == "returning":
            break
        time.sleep(.2)
    assert state and state.get("offboard", {}).get("status") == "returning", \
        f"Backend loss did not trigger Offboard watchdog RTL: {state}"
    station = created["simulation"]["pose"]["position"]
    wait_hub_landed_at_station(flight, instance_id, hub_observer, created["name"], station)


def test_real_offboard_drone_reset_invalidates_old_session_generation(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    model = created["name"]
    old_instance = flight.instance_ids[drone_id]
    old_generation = flight.request("GET", f"/api/v1/drones/{drone_id}/flight").json()["generation"]
    original_pose = flight.gazebo.current(model)["position"]
    initial = altitude(event_data(flight.observers[-1], "telemetry")[0])
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    ws = control_socket(drone_id, session)
    flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                   expected=(202,), json={"request_id": str(uuid.uuid4())})
    seq = drive(ws, 0, .8, up=.6)
    seq, airborne = drive_to_altitude(ws, seq, flight.observers[-1], initial,
                                      {"up": .6}, target_delta=2)
    assert altitude(airborne) >= initial + 2
    navsat_topic = next(sensor["topic"] for sensor in created["simulation"]["sensors"]
                        if sensor["type"] == "navsat")
    navsat = flight.observe_navsat(navsat_topic)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as executor:
        reset_request = executor.submit(httpx.post, BACKEND + f"/api/v1/drones/{drone_id}/reset",
                                        timeout=120)
        while not reset_request.done():
            flight.archive_live_px4_instances("drone-reset-startup", {old_instance})
            time.sleep(1)
        response = reset_request.result()
    flight.command_log.append({"kind": "gazebo_navsat_samples", "topic": navsat_topic,
                               "samples": navsat.snapshot()})
    flight.command_log.append({"monotonic": started, "method": "POST",
                               "path": f"/api/v1/drones/{drone_id}/reset",
                               "status": response.status_code, "response": response.json()})
    assert response.status_code == 200, f"drone reset failed: {response.status_code} {response.text}"
    reset = response.json()
    assert reset["id"] == drone_id and reset["status"] == "ready", reset
    gaz = flight.request("GET", f"http://gazebo-service:8000/api/v1/drones/{reset['simulation']['drone_id']}/").json()
    new_model = gaz.get("gazebo_model")
    assert new_model and new_model != model, f"reset reused Gazebo model name: {gaz}"
    flight.gazebo.tracked_names.add(new_model)
    with pytest.raises(Exception):
        ws.recv(timeout=2)
    ws.close()

    autopilot = flight.request("GET", f"/api/v1/drones/{drone_id}/autopilot").json()
    new_instance = autopilot["id"]
    assert new_instance != old_instance, "drone reset reused the old PX4 instance"
    flight.instance_ids[drone_id] = new_instance
    state = flight.wait_ready(drone_id)
    assert state["generation"] != old_generation
    restored = flight.gazebo.current(new_model)["position"]
    assert math.dist((restored["x"], restored["y"], restored["z"]),
                     (original_pose["x"], original_pose["y"], original_pose["z"])) <= .5, \
        f"drone reset did not restore its initial Gazebo pose: {restored} vs {original_pose}"
    flight.request("GET", f"http://px4-hub:8002/api/v1/instances/{old_instance}/flight",
                   expected=(404,))

    # The same public drone is controllable after reset with a new session.
    fresh = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                           expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    flight.request("DELETE", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{fresh['session_id']}",
                   expected=(202,))


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


def test_real_offboard_body_axes_yaw_and_default_speed_limits(flight):
    created = flight.create_drone()
    drone_id, model = created["id"], created["name"]
    observer = flight.observers[-1]
    initial_alt = altitude(event_data(observer, "telemetry")[0])
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    assert session["limits"]["horizontal_m_s"] == pytest.approx(3)
    assert session["limits"]["up_m_s"] <= 1
    assert session["limits"]["down_m_s"] <= 1
    assert session["limits"]["yaw_deg_s"] <= 60
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, 2.5)
        seq, airborne = drive_to_altitude(ws, seq, observer, initial_alt,
                                          {"up": .7}, target_delta=4)
        assert altitude(airborne) > initial_alt + 4
        vertical = observer.telemetry()["velocity"].get("down_m_s", 0)
        assert abs(float(vertical)) <= session["limits"]["up_m_s"] * 1.2
        seq, forward_start, forward_end = command_and_pose(flight, ws, seq, model, 1.5, forward=.45)
        seq, right_start, right_end = command_and_pose(flight, ws, seq, model, 1.5, right=.45)
        seq, yaw_start, turned = command_and_pose(flight, ws, seq, model, 2, yaw=.9)
        seq, turned_forward_start, turned_forward_end = command_and_pose(
            flight, ws, seq, model, 1.5, forward=.45)
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
        assert math.radians(60) < abs(yaw_delta) < math.radians(130), \
            f"yaw input did not make the expected turn: {math.degrees(yaw_delta):.1f} degrees"
        tx, ty = displacement(turned_forward_start, turned_forward_end)
        turned_forward_proj = (tx * math.cos(yaw_from_pose(turned_forward_start))
                               + ty * math.sin(yaw_from_pose(turned_forward_start)))
        assert turned_forward_proj > .3, \
            f"forward input after yaw did not follow the new body heading: {(tx, ty)}"
        bx, by = displacement(reverse_start, reverse_end)
        reverse_proj = bx * math.cos(yaw_from_pose(reverse_start)) + by * math.sin(yaw_from_pose(reverse_start))
        assert reverse_proj < -.3, f"negative forward input did not move backward: {(bx, by)}"
        lx, ly = displacement(left_start, left_end)
        left_proj = -lx * math.sin(yaw_from_pose(left_start)) + ly * math.cos(yaw_from_pose(left_start))
        assert left_proj > .3, f"negative right input did not move left: {(lx, ly)}"
        assert lowered["position"]["z"] < up_start["position"]["z"] - .3
        assert lowered["position"]["z"] > created["simulation"]["pose"]["position"]["z"] + 1

        # Observe diagonal speed after the command transition. This uses the
        # standard MVP parameters; altered PX4 parameter behavior is tested separately.
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


def test_real_offboard_reduced_px4_speed_limits(flight):
    """Recommended extended check; not part of the MVP default-parameter suite."""
    created = flight.create_drone()
    drone_id = created["id"]
    observer = flight.observers[-1]
    initial_alt = altitude(event_data(observer, "telemetry")[0])
    parameters = flight.request("PATCH", f"/api/v1/drones/{drone_id}/autopilot/parameters",
        expected=(200,), json={"MPC_XY_VEL_MAX": 1.2, "MPC_Z_VEL_MAX_UP": .6,
                               "MPC_Z_VEL_MAX_DN": .6}).json()
    assert parameters["parameters"]["MPC_XY_VEL_MAX"] == pytest.approx(1.2)
    session = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions",
                             expected=(201,), json={"request_id": str(uuid.uuid4())}).json()
    assert session["limits"]["horizontal_m_s"] == pytest.approx(1.2)
    ws = control_socket(drone_id, session)
    try:
        flight.request("POST", f"/api/v1/drones/{drone_id}/flight/offboard/sessions/{session['session_id']}/arm",
                       expected=(202,), json={"request_id": str(uuid.uuid4())})
        seq = drive(ws, 0, 2.5)
        seq, _, raised = command_and_pose(flight, ws, seq, created["name"], 6, up=.7)
        assert raised["position"]["z"] > initial_alt + 1.2
    finally:
        ws.close()
    # In case the reduced-limit climb succeeds, always request safe return.
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
    station = flight.native_geodetic(created["simulation"]["pose"]["position"])
    # DELETE is asynchronous: confirm fresh disarm/ON_GROUND before reserving
    # another controller, including the branch where the vehicle never took off.
    assert flight.wait_landed(drone_id, station)["armed"] is False

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


def assert_rejected_input_does_not_extend_watchdog(flight, invalid_message):
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
        ws.send(__import__("json").dumps(invalid_message(seq)))
        error = __import__("json").loads(ws.recv(timeout=2))
        assert error.get("type") == "error" and error.get("code") == "invalid_offboard_input", error
        rejected_at = time.monotonic()
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


def test_real_offboard_duplicate_sequence_does_not_extend_watchdog(flight):
    assert_rejected_input_does_not_extend_watchdog(flight, lambda seq: {
        "seq": seq - 1, "forward": .8, "right": 0, "up": 0, "yaw": 0})


def test_real_offboard_invalid_axis_does_not_extend_watchdog(flight):
    assert_rejected_input_does_not_extend_watchdog(flight, lambda seq: {
        "seq": seq, "forward": 1.1, "right": 0, "up": 0, "yaw": 0})


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


def test_real_mission_continues_after_backend_loss(flight):
    created = flight.create_drone()
    drone_id = created["id"]
    instance_id = flight.instance_ids[drone_id]
    hub_observer = flight.hub_telemetry(instance_id)
    waypoints = [{"x": 16, "y": -8, "z": 6}, {"x": 16, "y": -4, "z": 8},
                 {"x": 12, "y": -4, "z": 7}, {"x": 8, "y": -8, "z": 6}]
    mission = flight.mission(f"backend-loss-{uuid.uuid4().hex[:8]}", waypoints)
    accepted = flight.request("POST", f"/api/v1/drones/{drone_id}/flight/missions", expected=(202,), json={
        "mission_id": mission["id"], "revision": mission["revision"],
        "request_id": str(uuid.uuid4())}).json()

    # Backend is unavailable briefly while PX4 executes the uploaded route.
    FlightEnvironment.request_backend_restart("mission")
    assert flight.request("GET", "/api/v1/drones/").json() == [], \
        "Backend restart unexpectedly restored its in-memory public drone registry"
    deadline = time.monotonic() + MISSION_TIMEOUT
    execution = None
    while time.monotonic() < deadline:
        execution = hub_execution(instance_id, accepted["execution_id"])
        if execution.get("status") in {"completed", "failed", "interrupted"}:
            break
        time.sleep(.25)
    assert execution and execution.get("status") == "completed", \
        f"Mission did not complete autonomously across Backend loss: {execution}"

    reached = 0
    for record in flight.gazebo.records:
        if record["name"] != created["name"]:
            continue
        pose = record["position"]
        target = waypoints[reached]
        if (math.hypot(pose["x"] - target["x"], pose["y"] - target["y"]) <= 2
                and abs(pose["z"] - target["z"]) <= 1.5):
            reached += 1
            if reached == len(waypoints):
                break
    assert reached == len(waypoints), \
        f"Mission did not physically visit all waypoints during Backend outage ({reached}/4)"
    wait_hub_landed_at_station(flight, instance_id, hub_observer, created["name"],
                               created["simulation"]["pose"]["position"])


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
    attitude = flight.observers[-1].telemetry(required=("attitude",))["attitude"]
    gazebo_yaw = math.degrees(yaw_from_pose(flight.gazebo.current(created["name"])))
    expected_yaw = 90 - changed["heading_deg"] - gazebo_yaw
    yaw_error = (attitude["yaw_deg"] - expected_yaw + 180) % 360 - 180
    assert abs(yaw_error) < 15, f"PX4 heading disagrees with Gazebo before arm: {yaw_error:.1f} degrees"
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
    reached = 0
    for pose in pose_records:
        waypoint = points[reached]
        if (math.hypot(pose["x"] - waypoint["x"], pose["y"] - waypoint["y"]) <= 2
                and abs(pose["z"] - waypoint["z"]) <= 1.5):
            reached += 1
            if reached == len(points):
                break
    assert reached == len(points), f"Changed-origin route physically reached {reached}/4 waypoints in order"
    assert flight.wait_landed(created["id"], flight.native_geodetic(created["simulation"]["pose"]["position"]))["armed"] is False
    ground = created["simulation"]["pose"]["position"]
    landed_pose = flight.gazebo.current(created["name"])["position"]
    assert math.hypot(landed_pose["x"] - ground["x"], landed_pose["y"] - ground["y"]) <= 3, landed_pose


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
    mission_drone = flight.create_drone(pad_id="landing_pad_01")
    offboard_drone = flight.create_drone(pad_id="landing_pad_02")
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
