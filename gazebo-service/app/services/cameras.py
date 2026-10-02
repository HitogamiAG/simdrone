from __future__ import annotations
import logging, threading, time
from ..config import ROOT, LOCAL_ROOT
from ..errors import ApiFault
from ..geometry import Pose, Quaternion, Vector3
from ..entities import DroneRecord, SensorRecord
from ..api.schemas import DroneCreate, SensorPatch, WorldPatch
from ..gazebo.state import SerializedStepMap, decode_world, model_sdf
from .common import serialized, proto_dict
log = logging.getLogger("gazebo-service")

from .base import ServiceContext
from ..media.encoder import EncoderSession

class CamerasOperations(ServiceContext):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.camera_procs = {}
        self.encoder_sessions = {}

    @serialized
    def activate_camera(self, drone_id, sensor_name):
        sensor = self._sensor(drone_id, sensor_name)
        if not sensor.is_camera:
            raise ApiFault(422, "not_a_camera", "activate is available for camera sensors only")
        if sensor.id in self.camera_procs:
            return self._sensor_json(sensor)
        image_cls = self.world.sensor_types.get("gz.msgs.Image")
        if not image_cls:
            raise ApiFault(503, "binding_unavailable", "Gazebo Image binding is unavailable")
        width = height = step = None
        received = threading.Event()
        first = {}
        def probe(msg):
            if not first:
                first.update(width=msg.width, height=msg.height, step=msg.step, pixel=msg.pixel_format_type)
                received.set()
        if not self.world.subscribe(image_cls, sensor.topic, probe):
            raise ApiFault(503, "subscription_failed", f"Cannot subscribe to {sensor.topic}")
        try:
            if not received.wait(self.settings.sensor_sample_timeout):
                raise ApiFault(504, "sensor_timeout", "Camera produced no image")
        finally:
            self.world.unsubscribe(sensor.topic)
        width, height, step = first["width"], first["height"], first["step"]
        pixel = first["pixel"]
        if pixel not in (3, 8):
            raise ApiFault(422, "unsupported_pixel_format", f"Unsupported image pixel format: {pixel}")
        self.settings.media_dir.mkdir(parents=True, exist_ok=True)
        path = f"drones/{drone_id}/sensors/{sensor_name}"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-f", "rawvideo", "-pixel_format", "rgb24" if pixel == 3 else "bgr24", "-video_size", f"{width}x{height}", "-framerate", "25", "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", self.settings.ffmpeg_preset, "-tune", "zerolatency", "-f", "rtsp", "-rtsp_transport", "udp", f"rtsp://mediamtx:8554/{path}"]
        session = EncoderSession(self.world, sensor, image_cls, cmd, width, height, pixel,
                                 self._publish_sample)
        try:
            proc = session.start()
        except Exception as exc:
            raise ApiFault(503, "subscription_failed", f"Cannot start camera publishing: {exc}") from exc
        self.encoder_sessions[sensor.id] = session
        self.camera_procs[sensor.id] = proc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read(2048).decode(errors="replace") if proc.stderr else ""
                self.deactivate_camera(drone_id, sensor_name)
                raise ApiFault(502, "encoder_failed", err or "FFmpeg exited before publishing")
            try:
                import httpx
                api_auth = (self.settings.mediamtx_api_user, self.settings.mediamtx_api_password)
                resp = httpx.get(f"http://mediamtx:9997/v3/paths/get/{path}", auth=api_auth, timeout=.5)
                if resp.status_code == 200:
                    return self._sensor_json(sensor)
            except Exception:
                pass
            time.sleep(.2)
        # RTSP publish can take a little longer than the API path visibility.
        if proc.poll() is None:
            return self._sensor_json(sensor)
        self.deactivate_camera(drone_id, sensor_name)
        raise ApiFault(504, "publish_timeout", "MediaMTX did not accept the camera stream")

    @serialized
    def deactivate_camera(self, drone_id, sensor_name):
        sensor_id = f"{drone_id}:{sensor_name}"
        sensor = self.sensors.get(sensor_id)
        if sensor_id not in self.camera_procs:
            return self._sensor_json(sensor) if sensor else {"active": False}
        session = self.encoder_sessions.pop(sensor_id, None)
        self.camera_procs.pop(sensor_id, None)
        if session:
            session.stop()
        if sensor and sensor.topic in self.subscriptions:
            self.subscriptions.pop(sensor.topic, None)
        return self._sensor_json(sensor) if sensor else {"active": False}

    def _stop_all_cameras(self):
        for key in list(self.camera_procs):
            drone_id, sensor = key.split(":", 1)
            self.deactivate_camera(drone_id, sensor)
