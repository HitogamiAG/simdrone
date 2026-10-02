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
from .common import serialized, proto_dict, _settings_match
log = logging.getLogger("gazebo-service")

from .base import ServiceContext

class WorldOperations(ServiceContext):
    @serialized
    def world_info(self):
        scene = self._scene()
        snapshot = self._snapshot()
        current = decode_world(snapshot, self.settings.world_name)
        self.physics_values = current["physics"]
        self.spherical_values = current["spherical_coordinates"] or {}
        stats = proto_dict(snapshot.stats)
        exported_sdf = None
        export_error = None
        try:
            config_type = self.world.message_type("SdfGeneratorConfig")
            response = self.world.request(self.world.service("generate_world_sdf"), config_type(), config_type, self.world.message_type("StringMsg"))
            exported_sdf = response.data
        except Exception as exc:
            export_error = str(exc)
        models = []
        for model in scene.model:
            try:
                pose = self._current_pose(model.id)
                models.append({"name": model.name, "entity_id": model.id, "pose": pose.model_dump(), "source": "Gazebo pose/info", "fresh": True})
            except ApiFault:
                models.append({"name": model.name, "entity_id": model.id, "pose": None, "source": "Gazebo pose/info", "fresh": False})
        return {
            "name": self.settings.world_name,
            "physics": current["physics"], "gravity": current["gravity"],
            "magnetic_field": current["magnetic_field"],
            "atmosphere": {"value": getattr(self, "atmosphere", None), "source": "startup SDF", "fresh": False},
            "spherical_coordinates": current["spherical_coordinates"],
            "scene": proto_dict(current["scene"]) if current["scene"] else None,
            "models": models, "lights": [proto_dict(light) for light in scene.light],
            "simulation": {"paused": snapshot.stats.paused, "sim_time": proto_dict(snapshot.stats.sim_time),
                           "real_time_factor": snapshot.stats.real_time_factor, "statistics": stats},
            "current_sdf": {"value": exported_sdf, "source": self.world.service("generate_world_sdf"),
                            "fresh": False, "available": exported_sdf is not None, "error": export_error,
                            "note": "Gazebo SDF export includes original world settings; use component state for live values"},
            "sources": {key: {"source": self.world.service("state"), "fresh": True,
                              "sim_time": proto_dict(snapshot.stats.sim_time)}
                        for key in ("physics", "gravity", "spherical_coordinates", "magnetic_field", "scene")},
            "capabilities": {"mutable": ["physics.max_step_size", "physics.real_time_factor", "gravity", "spherical_coordinates"],
                "read_only": {"magnetic_field": "Gazebo Harmonic set_physics does not apply magnetic_field",
                              "atmosphere": "no world-level runtime Transport setter",
                              "scene/environment": "not implemented by this API",
                              "physics.real_time_update_rate": "not applied by Gazebo Harmonic PhysicsCmd",
                              "physics.backend": "selected while loading world"},
                "gazebo_services": [service for service in self.world.service_list() if f"/world/{self.settings.world_name}/" in service]},
        }

    @staticmethod
    def _settings_match(actual, expected):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(_settings_match(actual.get(key), value) for key, value in expected.items())
        if isinstance(expected, (list, tuple)):
            return isinstance(actual, (list, tuple)) and len(actual) == len(expected) and all(_settings_match(a, b) for a, b in zip(actual, expected))
        if isinstance(expected, (int, float)):
            return actual is not None and math.isclose(actual, expected, rel_tol=1e-7, abs_tol=1e-7)
        return actual == expected

    def _confirm_settings(self, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = decode_world(self._snapshot(), self.settings.world_name)
            if self._settings_match(current, expected):
                return current
            time.sleep(.05)
        raise ApiFault(504, "gazebo_timeout", "World changes were not observed", {"expected": expected})

    @serialized
    def patch_world(self, patch: WorldPatch):
        self._ensure_ready()
        snapshot = self._snapshot()
        current = decode_world(snapshot, self.settings.world_name)
        before = snapshot.stats.paused
        changed = []
        requested = []
        restore_error = None
        try:
            if not before:
                self.set_paused(True)
            if patch.physics or patch.gravity:
                merged = {**current["physics"], **(patch.physics or {})}
                expected = {"physics": merged}
                if patch.gravity:
                    expected["gravity"] = list(patch.gravity.values())
                requested = [f"physics.{key}" for key in (patch.physics or {})]
                if patch.gravity:
                    requested.append("gravity")
                self.world.set_physics(**merged, gravity=patch.gravity.values() if patch.gravity else None)
                self._confirm_settings(expected)
                changed.extend(requested)
            if patch.spherical_coordinates:
                values = {**(current["spherical_coordinates"] or {"surface_model": "EARTH_WGS84", "latitude_deg": 0, "longitude_deg": 0, "elevation": 0, "heading_deg": 0}), **patch.spherical_coordinates}
                requested = ["spherical_coordinates"]
                self.world.set_spherical_coordinates(**values)
                self._confirm_settings({"spherical_coordinates": values})
                changed.append("spherical_coordinates")
        except Exception as exc:
            raise ApiFault(503, "gazebo_command_failed", str(exc), {"applied": changed, "unconfirmed": [key for key in requested if key not in changed]}) from exc
        finally:
            if not before:
                try:
                    self.set_paused(False)
                except Exception as exc:
                    restore_error = str(exc)
        if restore_error:
            raise ApiFault(503, "pause_restore_failed", "World changes applied but running state could not be restored", {"applied": changed, "reason": restore_error})
        return {"applied": changed, "world": self.world_info()}

    @serialized
    def set_paused(self, paused: bool):
        self._ensure_ready()
        self.world.pause() if paused else self.world.resume()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = self.world.get_stats(750).paused
            except TimeoutError:
                continue
            if state is paused:
                return {"paused": paused}
            time.sleep(.05)
        raise ApiFault(504, "gazebo_timeout", f"Gazebo did not enter {'paused' if paused else 'running'} state")
