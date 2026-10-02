from __future__ import annotations
import asyncio, json, logging, math, os, subprocess, threading, time, uuid, xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from ..config import ROOT, LOCAL_ROOT
from ..errors import ApiFault
from ..geometry import Pose, Quaternion, Vector3
from ..entities import DroneRecord, SensorRecord
from ..api.schemas import DroneCreate, DronePatch, SensorPatch, WorldPatch
from ..gazebo.state import SerializedStepMap, decode_world, model_sdf
from .common import serialized, proto_dict
log = logging.getLogger("gazebo-service")

from .base import ServiceContext

class DronesOperations(ServiceContext):
    def _record(self, drone_id: str) -> DroneRecord:
        record = self.drones.get(drone_id)
        if not record:
            raise ApiFault(404, "drone_not_found", f"Drone {drone_id} was not found")
        return record

    @serialized
    def list_drones(self):
        scene = self._scene()
        by_name = {m.name: m for m in scene.model}
        result = []
        for rec in self.drones.values():
            model = by_name.get(rec.name)
            if model is None:
                continue
            rec.entity_id = model.id
            rec.pose = self._current_pose(model.id)
            self._refresh_sensors(rec.id, rec.name)
            result.append(self._drone_json(rec))
        return result

    def _drone_json(self, rec):
        from ..api.presenters import drone_json
        attached = [sensor for sensor in self.sensors.values() if sensor.id.startswith(rec.id + ":")]
        return drone_json(rec, attached, self._sensor_json)

    @serialized
    def create_drone(self, body: DroneCreate):
        with self.lock:
            self._ensure_ready()
            path = self.models.get(body.model)
            if not path or not path.is_file():
                raise ApiFault(422, "unknown_model", f"Model {body.model!r} is not installed", {"available": sorted(self.models)})
            name = body.name or f"{body.model}_{uuid.uuid4().hex[:8]}"
            if any(r.name == name for r in self.drones.values()):
                raise ApiFault(409, "name_conflict", f"Drone name {name!r} already exists")
            rec = DroneRecord(str(uuid.uuid4()), name, body.model, path, body.pose, body.pose.model_copy(deep=True), True)
            self._spawn(rec)
            rec.initial_sdf = model_sdf(self._snapshot(), rec.entity_id)
            if not rec.initial_sdf:
                raise ApiFault(503, "model_description_unavailable", "Gazebo did not expose the created model SDF")
            self.drones[rec.id] = rec
            self._refresh_sensors(rec.id, rec.name)
            return self._drone_json(rec)

    def _spawn(self, rec: DroneRecord):
        if rec.initial_sdf:
            self.world.create_model(rec.name, rec.pose, sdf=rec.initial_sdf)
        elif rec.path and rec.path.is_file():
            self.world.create_model(rec.name, rec.pose, sdf_filename=str(rec.path))
        else:
            raise ApiFault(409, "reset_unsupported", "No saved model description is available")
        model = self._wait_model(rec.name)
        rec.entity_id = model.id
        rec.pose = self._current_pose(model.id)

    def _wait_model(self, name):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(m.name == name for m in self._scene().model):
                return next(m for m in self._scene().model if m.name == name)
            time.sleep(.1)
        raise ApiFault(504, "gazebo_timeout", f"Model {name} did not appear in Gazebo")

    @staticmethod
    def _fill_pose(proto, pose):
        proto.name = ""
        proto.position.x, proto.position.y, proto.position.z = pose.position.values()
        proto.orientation.x, proto.orientation.y = pose.orientation.x, pose.orientation.y
        proto.orientation.z, proto.orientation.w = pose.orientation.z, pose.orientation.w

    @serialized
    def get_drone(self, drone_id):
        rec = self._record(drone_id)
        model = next((m for m in self._scene().model if m.name == rec.name), None)
        if model is None:
            raise ApiFault(404, "drone_not_found", f"Drone entity {rec.name} no longer exists")
        rec.entity_id = model.id
        rec.pose = self._current_pose(model.id)
        self._refresh_sensors(rec.id, rec.name)
        return self._drone_json(rec)

    @serialized
    def patch_drone(self, drone_id, body: DronePatch):
        self._ensure_ready()
        before = self._snapshot().stats.paused
        try:
            if not before:
                self.set_paused(True)
            return self._set_drone_pose(drone_id, body)
        finally:
            if not before:
                self.set_paused(False)

    def _set_drone_pose(self, drone_id, body):
        self.get_drone(drone_id)
        rec = self._record(drone_id)
        pose = self.world.message_type("Pose")()
        self._fill_pose(pose, body.pose)
        pose.id, pose.name = rec.entity_id, rec.name
        with self.pose_condition:
            sequence = self.pose_sequence
        self.world.set_pose(pose)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            actual = self._current_pose(rec.entity_id, after=sequence, timeout=max(.01, deadline-time.monotonic()))
            if self._pose_matches(actual, body.pose):
                rec.pose = actual
                return self._drone_json(rec)
            with self.pose_condition:
                sequence = self.pose_sequence
        raise ApiFault(504, "pose_timeout", "Gazebo did not apply the requested position and orientation", {"entity_id": rec.entity_id, "requested": body.pose.model_dump()})

    def _retire_drone_sensors(self, drone_id, reason):
        for sensor in list(self.sensors.values()):
            if not sensor.id.startswith(drone_id + ":"):
                continue
            if sensor.is_camera:
                self.deactivate_camera(drone_id, sensor.name)
            with sensor.guard:
                sensor.retired = True
                sensor.close_reason = reason
                sensor.epoch += 1
                sensor.latest = None
            self._unsubscribe_sensor(sensor)
            self.sensors.pop(sensor.id, None)

    def _wait_sensors(self, drone_id, names):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._refresh_sensors(drone_id, self.drones[drone_id].name)
            if all((sensor := self.sensors.get(f"{drone_id}:{name}")) and sensor.publisher_type for name in names):
                return
            time.sleep(.1)
        raise ApiFault(504, "sensor_timeout", "Recreated model sensors did not become ready", {"sensors": names})

    @serialized
    def reset_drone(self, drone_id):
        self._ensure_ready()
        rec = self._record(drone_id)
        if not rec.initial_sdf:
            raise ApiFault(409, "reset_unsupported", "No saved initial model description; model was not removed")
        # Validate the saved description before stopping or removing anything.
        root = ET.fromstring(rec.initial_sdf)
        if root.find("model") is None:
            raise ApiFault(409, "reset_unsupported", "Saved SDF does not contain a model; model was not removed")
        active = [s.name for s in self.sensors.values() if s.id.startswith(rec.id + ":") and s.is_camera and s.id in self.camera_procs]
        self._retire_drone_sensors(drone_id, "Drone reset; reconnect to the new sensor")
        old_entity = rec.entity_id
        self._remove_entity(rec.name)
        with self.pose_condition:
            self.poses.pop(old_entity, None)
        rec.pose = rec.initial_pose.model_copy(deep=True)
        try:
            self._spawn(rec)
        except Exception as exc:
            self.drones.pop(drone_id, None)
            raise ApiFault(503, "drone_recreate_failed", "Old model removed but recreation failed", {"id": drone_id, "removed": True, "reason": str(exc)}) from exc
        self._refresh_sensors(rec.id, rec.name)
        if active:
            self._wait_sensors(drone_id, active)
            for name in active:
                self.activate_camera(drone_id, name)
        return self._drone_json(rec)

    @serialized
    def delete_drone(self, drone_id):
        self._ensure_ready()
        rec = self._record(drone_id)
        self._retire_drone_sensors(drone_id, "Drone deleted")
        self._remove_entity(rec.name)
        with self.pose_condition:
            self.poses.pop(rec.entity_id, None)
        del self.drones[drone_id]
        return {"deleted": True, "id": drone_id}

    def _remove_entity(self, name):
        self.world.remove_model(name)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(m.name == name for m in self._scene().model):
                return
            time.sleep(.05)
        raise ApiFault(504, "gazebo_timeout", "Model removal was not observed", {"name": name})
