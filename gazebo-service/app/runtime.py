from __future__ import annotations
import json, logging, math, os, threading, time, uuid, xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from google.protobuf import json_format
from .config import LOCAL_ROOT, ROOT, Settings
from .catalog import ModelCatalog
from .spawn_pads import read_spawn_pads
from .errors import ApiFault
from .geometry import Pose, Quaternion, Vector3
from .entities import DroneRecord, SensorRecord
from .api.schemas import DroneCreate, SensorPatch, WorldPatch
from .gazebo.state import SerializedStepMap, decode_world, model_sdf
from .gazebo.client import GazeboClient
from .gazebo.process import GazeboProcess
from .gazebo.poses import PoseTracker
from .services.world import WorldOperations
from .services.drones import DronesOperations
from .services.sensors import SensorsOperations
from .services.cameras import CamerasOperations

log = logging.getLogger("gazebo-service")
def proto_dict(msg: Any) -> dict[str, Any]:
    return json_format.MessageToDict(msg, preserving_proto_field_name=True)
def read_sdf(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise RuntimeError(f"Cannot read world SDF {path}: {exc}") from exc

class SessionState:
    """Mutable registries shared by services for one server generation."""
    def __init__(self):
        self.lock = threading.RLock()
        self.generation = 0
        self.models: dict[str, Path] = {}
        self.drones: dict[str, DroneRecord] = {}
        self.sensors: dict[str, SensorRecord] = {}
        self.spawn_pads: list[dict] = []
        self.physics_values: dict[str, Any] = {}
        self.spherical_values: dict[str, Any] = {}


class RuntimeCoordinator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.state = SessionState()
        self.catalog = ModelCatalog(self.settings)
        self.models = self.catalog.models()
        self.world_operations = WorldOperations(self)
        self.drone_operations = DronesOperations(self)
        self.sensor_operations = SensorsOperations(self)
        self.camera_operations = CamerasOperations(self)
        self.world: GazeboClient | None = None
        self.process_manager = GazeboProcess(self.settings.gazebo_log_file)
        self._closed = False
        self.pose_tracker: PoseTracker | None = None

    _STATE_FIELDS = {"generation", "models", "drones", "sensors", "physics_values", "spherical_values", "spawn_pads"}

    def __setattr__(self, name, value):
        state = self.__dict__.get("state")
        if name in self._STATE_FIELDS and state is not None:
            setattr(state, name, value)
            return
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        state_names = self._STATE_FIELDS | {"lock", "subscriptions"}
        if name in state_names:
            state = self.__dict__.get("state")
            if state is not None and hasattr(state, name):
                return getattr(state, name)
        tracker = self.__dict__.get("pose_tracker")
        if tracker is not None:
            pose_names = {"pose_condition": "condition", "poses": "poses", "pose_sequence": "sequence"}
            if name in pose_names:
                return getattr(tracker, pose_names[name])
            pose_methods = {"_current_pose": "current", "_pose_matches": "matches", "_pose_from_proto": "from_proto"}
            if name in pose_methods:
                return getattr(tracker, pose_methods[name])
        for service_name in ("world_operations", "drone_operations", "sensor_operations", "camera_operations"):
            service = self.__dict__.get(service_name)
            if service is not None:
                try:
                    return object.__getattribute__(service, name)
                except AttributeError:
                    pass
                if name in getattr(service, "__dict__", {}):
                    return service.__dict__[name]
        raise AttributeError(name)


    def start(self):
        with self.lock:
            self._closed = False
            self._start_world()

    def _start_world(self):
        world_path = self.settings.world_path
        if not world_path.is_file():
            raise RuntimeError(f"world file does not exist: {world_path}")
        self.world = GazeboClient(self.settings.world_name, self.settings.request_timeout_ms)
        cmd = ["gz", "sim", "-v", self.settings.gazebo_verbosity, "--headless-rendering", self.settings.render_engine, "-s", "-r", str(world_path)]
        process = self.process_manager.start(cmd)
        try:
            self.world.set_process(process)
            self.world.wait_until_ready(int(self.settings.startup_timeout * 1000))
            self.pose_tracker = PoseTracker(self.world, self.settings.world_name, lambda: self.generation)
            self.pose_tracker.start()
            self._read_initial_config(world_path)
            try:
                self.spawn_pads = read_spawn_pads(world_path, self.settings.world_name, self.settings.model_roots)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            if not self.spawn_pads:
                raise RuntimeError("world must configure at least one drone spawn pad")
            scene_names = {model.name for model in self._scene().model}
            configured = {pad["id"] for pad in self.spawn_pads}
            missing = sorted(configured - scene_names)
            if missing:
                raise RuntimeError(f"spawn pads missing from Gazebo scene: {missing}")
            self._discover_existing()
            self._refresh_sensors()
        except Exception:
            self._stop_pose_subscription()
            self._stop_process()
            raise
        log.info("Gazebo world %s is ready", self.settings.world_name)

    def _read_initial_config(self, world_path: Path):
        root = read_sdf(world_path)
        world = root.find(f"world[@name='{self.settings.world_name}']")
        if world is None:
            raise RuntimeError(f"SDF does not contain world {self.settings.world_name}")
        physics = world.find("physics")
        def float_text(parent, name, default):
            node = parent.find(name) if parent is not None else None
            return float(node.text) if node is not None and node.text else default
        self.physics_values = {
            "max_step_size": float_text(physics, "max_step_size", 0.001),
            "real_time_factor": float_text(physics, "real_time_factor", 1.0),
        }
        grav = world.findtext("gravity", "0 0 -9.80665").split()
        self.physics_values["gravity"] = tuple(map(float, grav))
        sph = world.find("spherical_coordinates")
        self.spherical_values = {}
        if sph is not None:
            self.spherical_values = {
                "surface_model": sph.findtext("surface_model", "EARTH_WGS84"),
                "latitude_deg": float_text(sph, "latitude_deg", 0),
                "longitude_deg": float_text(sph, "longitude_deg", 0),
                "elevation": float_text(sph, "elevation", 0),
                "heading_deg": float_text(sph, "heading_deg", 0),
            }
        magnetic = world.find("magnetic_field")
        self.magnetic_field = [float(v) for v in magnetic.text.split()] if magnetic is not None and magnetic.text else None
        atmosphere = world.find("atmosphere")
        self.atmosphere = ET.tostring(atmosphere, encoding="unicode") if atmosphere is not None else None

    def _discover_existing(self):
        scene = self._scene()
        snapshot = self._snapshot()
        for model in scene.model:
            name = model.name
            if name in {"ground_plane", "sun"} or name in {pad["id"] for pad in self.spawn_pads}:
                continue
            if any(r.name == name for r in self.drones.values()):
                continue
            model_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.settings.world_name}/{name}"))
            pose = self._pose_from_proto(model.pose)
            self.drones[model_id] = DroneRecord(model_id, name, name, self.models.get(name), pose,
                pose.model_copy(deep=True), initial_sdf=model_sdf(snapshot, model.id), entity_id=model.id)

    def status(self):
        process_alive = self.process_manager.alive()
        transport_ready = False
        try:
            transport_ready = bool(process_alive and self.world and self.world.world_exists(self.settings.request_timeout_ms))
        except Exception:
            pass
        return {"process_alive": process_alive, "world_ready": transport_ready, "world": self.settings.world_name, "pid": self.process_manager.process.pid if process_alive else None}

    def drone_pose_sample(self, gazebo_drone_id: str):
        """Read a mapped drone pose from the cached pose/info subscription."""
        with self.lock:
            record = next((drone for drone in self.drones.values()
                           if drone.gazebo_model == gazebo_drone_id or drone.id == gazebo_drone_id), None)
            if record is None:
                raise ApiFault(404, "drone_not_found", "Drone was not found")
            tracker, entity_id, world_generation = self.pose_tracker, record.entity_id, self.generation
            public_id, name = record.id, record.name
        if tracker is None or entity_id is None:
            raise ApiFault(503, "pose_unavailable", "Gazebo pose subscription is unavailable")
        sample = tracker.latest(entity_id)
        if sample is None:
            raise ApiFault(503, "pose_unavailable", "Gazebo has not published a pose for this drone")
        return {"type": "pose", "drone_id": public_id, "name": name,
                "entity_id": entity_id, "world_generation": world_generation,
                "sequence": sample["sequence"], "age_s": sample["age_s"],
                "received_at": sample["received_at"], "pose": sample["pose"].model_dump()}

    def reboot(self):
        with self.lock:
            self.generation += 1
            self._stop_all_cameras()
            self._unsubscribe_all()
            self._stop_pose_subscription()
            self._stop_process()
            self.drones.clear()
            self.sensors.clear()
            self.subscriptions.clear()
            self._start_world()
            return self.status()

    def _stop_process(self):
        self.process_manager.stop()
        self.world = None

    def _stop_pose_subscription(self):
        if self.pose_tracker:
            self.pose_tracker.stop()
            self.pose_tracker = None

    def shutdown(self):
        with self.lock:
            self._closed = True
            self.generation += 1
            self._stop_all_cameras()
            self._unsubscribe_all()
            self._stop_pose_subscription()
            self._stop_process()

    def reset_world(self):
        return self.reboot()
