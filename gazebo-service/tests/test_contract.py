from fastapi.testclient import TestClient
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import threading

from app.api.schemas import WorldPatch
from app.geometry import Pose, Quaternion, Vector3
from app.main import app, create_app
from app.errors import ApiFault
from app.entities import DroneRecord, SensorRecord
from app.runtime import RuntimeCoordinator
from app.gazebo.poses import PoseTracker
from app.media.encoder import EncoderSession
from app.services.subscriptions import SensorSubscription
from app.gazebo.state import SerializedStepMap, component_id, decode_world, model_sdf
from app.catalog import ModelCatalog
from app.config import LOCAL_ROOT, Settings
from gz.msgs10.physics_pb2 import Physics


def test_default_world_and_flight_models_are_bundled_in_application():
    settings = Settings()
    assert settings.world_path == LOCAL_ROOT / "app/worlds/empty.sdf"
    assert settings.world_path.is_file()
    from xml.etree import ElementTree
    world = ElementTree.parse(settings.world_path).getroot().find("world")
    assert world.findtext("gravity") == "0 0 -9.80665"
    models = ModelCatalog(Settings(model_roots=(LOCAL_ROOT / "app/models",))).models()
    assert models["x500_gimbal"] == LOCAL_ROOT / "app/models/x500_gimbal/model.sdf"
    assert models["gimbal_camera"] == LOCAL_ROOT / "app/models/gimbal_camera/model.sdf"
    assert all(path.is_file() for path in (LOCAL_ROOT / "app/models/gimbal_camera/meshes").glob("*.stl"))
    from app.gazebo.world import _load_bindings
    # Gazebo Harmonic air pressure publishers use FluidPressure on Transport.
    bindings = _load_bindings()
    assert "gz.msgs.FluidPressure" in bindings["sensor_types"]
    assert {"Double", "SdfGeneratorConfig"} <= bindings.keys()


def test_pose_quaternion_is_normalized():
    pose = Pose(position=Vector3(x=1, y=2, z=3), orientation=Quaternion(x=0, y=0, z=2, w=2))
    assert pose.orientation.z == pose.orientation.w == 2 ** -0.5


def test_zero_quaternion_is_rejected():
    try:
        Quaternion(x=0, y=0, z=0, w=0)
    except ValueError:
        return
    raise AssertionError("zero quaternion should fail validation")


def test_world_patch_rejects_unsupported_magnetic_field():
    try:
        WorldPatch.model_validate({"magnetic_field": {"x": 1, "y": 2, "z": 3}})
    except ValueError:
        return
    raise AssertionError("unsupported field should fail validation")


def test_openapi_contains_public_endpoints():
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/world" in paths
    assert any(getattr(route, "path", "") == "/api/v1/drones/{drone_id}/sensors/{sensor_id}/stream" for route in app.routes)
    assert "/api/v1/world/list" not in paths
    assert "patch" not in paths["/api/v1/drones/{drone_id}/"]


def test_api_fault_uses_common_error_format(monkeypatch):
    def missing(_):
        from app.errors import ApiFault
        raise ApiFault(404, "drone_not_found", "missing")
    class FakeRuntime:
        def start(self): pass
        def shutdown(self): pass
        def get_drone(self, _): missing(_)
    test_app = create_app(runtime_factory=lambda _: FakeRuntime())
    with TestClient(test_app) as client:
        response = client.get("/api/v1/drones/nope/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "drone_not_found"


def test_pose_confirmation_checks_rotation_and_accepts_equivalent_quaternions():
    base = Pose(position=Vector3(x=1, y=2, z=3), orientation=Quaternion(x=0, y=0, z=0, w=1))
    rotated = base.model_copy(deep=True)
    rotated.orientation = Quaternion(x=0, y=0, z=1, w=0)
    assert not PoseTracker.matches(base, rotated)
    equivalent = base.model_copy(deep=True)
    equivalent.orientation.w = -1
    assert PoseTracker.matches(base, equivalent)


def test_pose_tracker_latest_is_nonblocking_fresh_and_returns_a_copy():
    tracker = PoseTracker.__new__(PoseTracker)
    tracker.condition = threading.Condition()
    tracker.poses = {17: (Pose(position=Vector3(x=1, y=2, z=3),
                               orientation=Quaternion(x=0, y=0, z=0, w=1)),
                          8, time.monotonic(), "2026-10-05T00:00:00+00:00")}
    sample = tracker.latest(17)
    assert sample["sequence"] == 8
    assert sample["age_s"] >= 0
    assert sample["received_at"] == "2026-10-05T00:00:00+00:00"
    sample["pose"].position.x = 99
    assert tracker.latest(17)["pose"].position.x == 1
    assert tracker.latest(18) is None


def test_pose_websocket_route_is_available():
    assert any(getattr(route, "path", "") == "/api/v1/drones/{drone_id}/pose/stream"
               and getattr(route, "name", "") == "drone_pose_stream" for route in app.routes)


def test_world_state_reads_components_instead_of_initial_sdf():
    snapshot = SerializedStepMap()
    entity = snapshot.state.entities[1]
    entity.id = 1
    values = {"World": b"-", "Name": b"empty", "Gravity": b"0 0 -8",
              "Physics": Physics(max_step_size=.006, real_time_factor=.7).SerializeToString()}
    for index, (name, value) in enumerate(values.items()):
        comp = entity.components[index]
        comp.type, comp.component = component_id(name), value
    actual = decode_world(snapshot, "empty")
    assert actual["physics"] == {"max_step_size": .006, "real_time_factor": .7}
    assert actual["gravity"] == [0, 0, -8]


def test_reset_without_saved_sdf_cannot_remove_model(monkeypatch):
    simulator = RuntimeCoordinator()
    pose = Pose(position=Vector3(x=0, y=0, z=1), orientation=Quaternion(x=0, y=0, z=0, w=1))
    simulator.drones["one"] = DroneRecord("one", "quad", "test_quad", None, pose, pose)
    monkeypatch.setattr(simulator.drone_operations, "_ensure_ready", lambda: None)
    removed = []
    monkeypatch.setattr(simulator.drone_operations, "_remove_entity", removed.append)
    with pytest.raises(ApiFault) as error:
        simulator.reset_drone("one")
    assert error.value.code == "reset_unsupported"
    assert removed == []


def test_sensor_reset_clears_cache_and_invalidates_queued_epoch(monkeypatch):
    simulator = RuntimeCoordinator()
    sensor = SensorRecord("one:imu", "imu", "imu", "/imu", initial_rate=50, latest={"old": True})
    monkeypatch.setattr(simulator.sensor_operations, "_sensor", lambda *_: sensor)
    monkeypatch.setattr(simulator.sensor_operations, "patch_sensor", lambda *_: {"latest": sensor.latest})
    before = sensor.epoch
    result = simulator.reset_sensor("one", "imu")
    assert sensor.latest is None and result["latest"] is None
    assert sensor.epoch > before


def test_retiring_sensors_marks_old_record_and_clears_cache(monkeypatch):
    simulator = RuntimeCoordinator()
    sensor = SensorRecord("one:imu", "imu", "imu", "/imu", latest={"old": True})
    simulator.sensors[sensor.id] = sensor
    simulator._retire_drone_sensors("one", "Drone deleted")
    assert sensor.retired and sensor.latest is None
    assert sensor.id not in simulator.sensors


def test_partial_spherical_patch_is_validated_before_transport():
    assert WorldPatch(spherical_coordinates={"heading_deg": 5}).spherical_coordinates == {"heading_deg": 5}
    with pytest.raises(ValueError):
        WorldPatch(spherical_coordinates={"surface_model": "invalid"})
    with pytest.raises(ValueError):
        WorldPatch(spherical_coordinates={"elevation": float("inf")})


def test_world_partial_failure_reports_confirmed_and_unconfirmed_fields(monkeypatch):
    import app.services.world as main
    simulator = RuntimeCoordinator()
    simulator.world = SimpleNamespace(set_physics=lambda **_: None, set_spherical_coordinates=lambda **_: None)
    monkeypatch.setattr(simulator.world_operations, "_ensure_ready", lambda: None)
    monkeypatch.setattr(simulator.world_operations, "_snapshot", lambda: SimpleNamespace(stats=SimpleNamespace(paused=True)))
    monkeypatch.setattr(main, "decode_world", lambda *_: {"physics": {"max_step_size": .004, "real_time_factor": 1}, "spherical_coordinates": {"surface_model": "EARTH_WGS84", "latitude_deg": 0, "longitude_deg": 0, "elevation": 0, "heading_deg": 0}})
    def confirm(expected):
        if "spherical_coordinates" in expected:
            raise ApiFault(504, "gazebo_timeout", "not observed")
    monkeypatch.setattr(simulator.world_operations, "_confirm_settings", confirm)
    with pytest.raises(ApiFault) as error:
        simulator.patch_world(WorldPatch(physics={"max_step_size": .005}, spherical_coordinates={"heading_deg": 12}))
    assert error.value.details == {"applied": ["physics.max_step_size"], "unconfirmed": ["spherical_coordinates"]}


class FakeEncoderProcess:
    class Stdin:
        def __init__(self): self.closed = 0
        def close(self): self.closed += 1
        def write(self, data): pass
    def __init__(self):
        self.stdin = self.Stdin()
        self.stderr = None
        self.waits = 0
    def poll(self): return 0
    def wait(self, timeout=None): self.waits += 1
    def terminate(self): pass
    def kill(self): pass


def test_encoder_session_closes_idempotently_and_honors_stride():
    class FakeClient:
        def __init__(self): self.unsubscribed = []
        def subscribe(self, *_): return True
        def unsubscribe(self, topic): self.unsubscribed.append(topic)
    class Sensor:
        id = "d1:cam"
        topic = "/camera"
    class Image:
        data = bytes([1, 2, 3, 99, 4, 5, 6, 99])
        step = 4
    assert EncoderSession.frame_bytes(Image(), 1, 2, 3) == bytes([3, 2, 1, 6, 5, 4])
    client, process = FakeClient(), FakeEncoderProcess()
    session = EncoderSession(client, Sensor(), object, ["ffmpeg"], 1, 2, 3, lambda *_: None,
                             popen=lambda *_args, **_kwargs: process)
    session.start()
    session.stop()
    session.stop()
    assert client.unsubscribed == ["/camera"]
    assert process.stdin.closed == 1 and process.waits == 1


def test_sensor_subscription_handle_closes_once_after_sensor_retirement():
    class FakeHub:
        def __init__(self): self.closed = []
        def unsubscribe_queue(self, sensor, queue): self.closed.append((sensor, queue))
    sensor = SensorRecord("drone:imu", "imu", "imu", "/imu")
    queue = object()
    hub = FakeHub()
    handle = SensorSubscription(hub, sensor, queue)
    sensor.retired = True
    sensor.close_reason = "Drone deleted"
    assert handle.retired and handle.close_reason == "Drone deleted"
    handle.close()
    handle.close()
    assert hub.closed == [(sensor, queue)]
