from __future__ import annotations
import asyncio, json, logging, math, os, subprocess, threading, time, uuid, xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from ..config import ROOT, LOCAL_ROOT
from ..errors import ApiFault
from ..geometry import Pose, Quaternion, Vector3
from ..entities import DroneRecord, SensorRecord
from ..api.schemas import DroneCreate, SensorPatch, WorldPatch
from ..gazebo.state import SerializedStepMap, decode_world, model_sdf
from .common import serialized, proto_dict
log = logging.getLogger("gazebo-service")

from .base import ServiceContext
from .subscriptions import SensorHub

class SensorsOperations(ServiceContext):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.hub = SensorHub(runtime)


    def _refresh_sensors(self, drone_id: str | None = None, name: str | None = None):
        if not self.world:
            return
        names = {r.name: r.id for r in self.drones.values()}
        if name and drone_id:
            names[name] = drone_id
        prefix_root = f"/world/{self.settings.world_name}/model/"
        try:
            topics = self.world.topic_list()
        except Exception:
            return
        for topic in topics:
            if not topic.startswith(prefix_root) or "/sensor/" not in topic or topic.endswith("/camera_info"):
                continue
            tail = topic[len(prefix_root):].split("/")
            try:
                model_name = tail[0]
                sensor_pos = tail.index("sensor")
                sensor_name, stream = tail[sensor_pos + 1], tail[-1]
            except (ValueError, IndexError):
                continue
            record_id = names.get(model_name)
            if record_id is None or stream not in {"image", "camera_info", "imu", "air_pressure", "navsat", "magnetometer"}:
                continue
            sid = f"{record_id}:{sensor_name}"
            record = self.sensors.get(sid)
            if record is None:
                kind = {"image": "camera", "air_pressure": "air_pressure"}.get(stream, stream)
                record = SensorRecord(sid, sensor_name, kind, topic, is_camera=stream == "image")
                model_record = self.drones.get(record_id)
                if model_record:
                    record.initial_rate = self.catalog.initial_sensor_rate(model_record.path, sensor_name, model_record.initial_sdf)
                    record.rate = record.initial_rate
                self.sensors[sid] = record
            elif stream == "image":
                record.kind = "camera"
                record.is_camera = True
                record.topic = topic
            if not record.publisher_type:
                try:
                    publishers, _ = self.world.topic_info(topic)
                    if publishers:
                        record.publisher_type = publishers[0].msg_type_name
                except Exception:
                    pass

    @staticmethod
    def _sample_stamp(msg):
        return proto_dict(msg.header.stamp) if msg.HasField("header") else None


    @property
    def subscriptions(self):
        return self.hub.subscriptions

    def _publish_sample(self, sensor, msg):
        return self.hub.publish_sample(sensor, msg)

    def _ensure_sensor_subscription(self, sensor):
        return self.hub.ensure_subscription(sensor)

    @serialized
    def subscribe_sensor(self, sensor, loop, queue):
        return self.hub.subscribe(sensor, loop, queue)

    @serialized
    def open_stream(self, drone_id, sensor_name, loop, queue):
        sensor = self._sensor(drone_id, sensor_name)
        return self.hub.subscribe(sensor, loop, queue)

    def _release_sensor_subscription(self, sensor):
        return self.hub.release_subscription(sensor)

    @serialized
    def unsubscribe_queue(self, sensor, queue):
        return self.hub.unsubscribe_queue(sensor, queue)

    def _unsubscribe_sensor(self, sensor):
        return self.hub.unsubscribe_sensor(sensor)

    def _unsubscribe_all(self):
        return self.hub.unsubscribe_all()

    @serialized
    def list_sensors(self, drone_id):
        self.get_drone(drone_id)
        return [self._sensor_json(s) for s in self.sensors.values() if s.id.startswith(drone_id + ":")]

    def _sensor_json(self, sensor):
        from ..api.presenters import sensor_json
        capabilities = ["activate", "deactivate"] if sensor.is_camera else ["stream"]
        if self.world and sensor.topic + "/set_rate" in self.world.service_list():
            capabilities.extend(["update_rate", "reset"])
        proc = self.camera_procs.get(sensor.id)
        drone_id, _, sensor_name = sensor.id.partition(":")
        stream_url = f"rtsp://{self.settings.mediamtx_rtsp_host}:{self.settings.rtsp_port}/drones/{drone_id}/sensors/{sensor_name}" if sensor.is_camera else None
        active = proc is not None and proc.poll() is None if sensor.is_camera else None
        return sensor_json(sensor, capabilities=capabilities, active=active, stream_url=stream_url)

    @serialized
    def get_sensor(self, drone_id, sensor_name):
        self.get_drone(drone_id)
        sensor = self.sensors.get(f"{drone_id}:{sensor_name}")
        if not sensor:
            raise ApiFault(404, "sensor_not_found", f"Sensor {sensor_name} was not found")
        return self._sensor_json(sensor)

    @serialized
    def patch_sensor(self, drone_id, sensor_name, body: SensorPatch):
        sensor = self._sensor(drone_id, sensor_name)
        rate_topic = sensor.topic + "/set_rate"
        if rate_topic not in self.world.service_list():
            raise ApiFault(422, "unsupported_field", "Gazebo did not advertise set_rate", {"field": "update_rate", "service": rate_topic})
        if sensor.initial_rate is not None and body.update_rate > sensor.initial_rate and sensor.initial_rate > 0:
            raise ApiFault(422, "invalid_sensor_rate", "update_rate cannot exceed the sensor's SDF maximum", {"maximum": sensor.initial_rate})
        if body.update_rate == 0 and sensor.initial_rate not in (None, 0):
            raise ApiFault(422, "invalid_sensor_rate", "update_rate must be positive for a sensor with an SDF rate")
        snapshot = self._snapshot()
        if snapshot.stats.paused:
            raise ApiFault(409, "simulation_paused", "Resume simulation to verify the sensor update rate; no command was sent")
        step = decode_world(snapshot, self.settings.world_name)["physics"]["max_step_size"]
        target_period = max(step, 1 / body.update_rate) if body.update_rate else step
        intervals = []
        previous = [None]
        command_sent = threading.Event()
        confirmed = threading.Event()
        def observe(sample):
            if not command_sent.is_set():
                return
            stamp = sample.get("sim_time")
            if stamp is None:
                return
            now = float(stamp.get("sec", 0)) + float(stamp.get("nsec", 0)) * 1e-9
            if previous[0] is not None:
                dt = now - previous[0]
                intervals.append(dt)
                tolerance = step * 1.1 + target_period * .01
                if len(intervals) >= 4 and all(abs(value-target_period) <= tolerance for value in intervals[-4:]):
                    confirmed.set()
            previous[0] = now
        with sensor.guard:
            sensor.observers.add(observe)
        try:
            self._ensure_sensor_subscription(sensor)
            double_type = self.world.message_type("Double")
            # SetRate is a void handler: its reply does not confirm application.
            # A short reply timeout avoids waiting several seconds for no payload.
            self.world.request_transport(rate_topic, double_type(data=body.update_rate), double_type, self.world.message_type("Empty"), 250)
            command_sent.set()
            timeout = max(5, target_period * 8 / max(snapshot.stats.real_time_factor, .1))
            if not confirmed.wait(min(timeout, 20)):
                sensor.rate = None
                sensor.rate_confirmed = False
                raise ApiFault(504, "sensor_rate_timeout", "Requested rate was not observed; command may have been applied", {"requested": body.update_rate, "intervals": intervals[-8:]})
            sensor.rate = body.update_rate
            sensor.rate_confirmed = True
            return self._sensor_json(sensor)
        finally:
            with sensor.guard:
                sensor.observers.discard(observe)
            self._release_sensor_subscription(sensor)

    @serialized
    def _sensor(self, drone_id, sensor_name):
        self._record(drone_id)
        self._refresh_sensors(drone_id, self.drones[drone_id].name)
        sensor = self.sensors.get(f"{drone_id}:{sensor_name}")
        if not sensor:
            raise ApiFault(404, "sensor_not_found", f"Sensor {sensor_name} was not found")
        return sensor

    @serialized
    def reset_sensor(self, drone_id, sensor_name):
        sensor = self._sensor(drone_id, sensor_name)
        if sensor.initial_rate is None:
            raise ApiFault(409, "reset_unsupported", "Initial sensor update_rate is unavailable; nothing was changed")
        result = self.patch_sensor(drone_id, sensor_name, SensorPatch(update_rate=sensor.initial_rate))
        with sensor.guard:
            sensor.epoch += 1
            sensor.latest = None
            sensor.last_stamp = None
            sensor.observed_rate = None
        result["latest"] = None
        result["observed_update_rate"] = None
        return result






