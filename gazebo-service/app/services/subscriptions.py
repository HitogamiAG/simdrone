"""Shared Transport subscriptions, samples, observers, and client queues."""
import asyncio
import logging
from ..errors import ApiFault
from .base import ServiceContext
from .common import proto_dict

log = logging.getLogger("gazebo-service")

class SensorSubscription:
    """Client-owned handle that stays valid after its sensor leaves the registry."""
    def __init__(self, hub, sensor, queue):
        self._hub = hub
        self._sensor = sensor
        self._queue = queue
        self._closed = False

    @property
    def sensor_name(self): return self._sensor.name
    @property
    def is_camera(self): return self._sensor.is_camera
    @property
    def latest(self): return self._sensor.latest
    @property
    def retired(self): return self._sensor.retired
    @property
    def epoch(self): return self._sensor.epoch
    @property
    def close_reason(self): return self._sensor.close_reason
    @property
    def queue(self): return self._queue

    def close(self):
        if self._closed: return
        self._closed = True
        self._hub.unsubscribe_queue(self._sensor, self._queue)


class SensorHub(ServiceContext):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.subscriptions = {}

    def publish_sample(self, sensor, msg):
        stamp = proto_dict(msg.header.stamp) if msg.HasField("header") else None
        sample = {"sensor_id": sensor.name, "type": sensor.kind, "sim_time": stamp}
        if not sensor.is_camera:
            sample["data"] = proto_dict(msg)
        with sensor.guard:
            if sensor.retired: return
            epoch = sensor.epoch
            sensor.latest = sample
            if stamp is not None:
                now = float(stamp.get("sec", 0)) + float(stamp.get("nsec", 0)) * 1e-9
                if sensor.last_stamp is not None and now > sensor.last_stamp:
                    sensor.observed_rate = 1 / (now - sensor.last_stamp)
                sensor.last_stamp = now
            queues = tuple(sensor.queues.items())
            observers = tuple(sensor.observers)
        for observer in observers:
            observer(sample)
        for queue, loop in queues:
            def publish_latest(q=queue, value=sample, version=epoch):
                with sensor.guard:
                    if sensor.retired or sensor.epoch != version or q not in sensor.queues: return
                if q.full():
                    try: q.get_nowait()
                    except asyncio.QueueEmpty: pass
                q.put_nowait((version, value))
            try: loop.call_soon_threadsafe(publish_latest)
            except RuntimeError: pass

    def ensure_subscription(self, sensor):
        if sensor.topic in self.subscriptions or sensor.id in self.camera_procs:
            return
        msg_type = self.world.sensor_types.get(sensor.publisher_type)
        if msg_type is None:
            raise ApiFault(422, "unsupported_sensor_type", f"No Python binding for {sensor.publisher_type}")
        if not self.world.subscribe(msg_type, sensor.topic, lambda msg: self.publish_sample(sensor, msg)):
            raise ApiFault(503, "subscription_failed", f"Cannot subscribe to {sensor.topic}")
        self.subscriptions[sensor.topic] = sensor

    def subscribe(self, sensor, loop, queue):
        if sensor.retired:
            raise ApiFault(409, "sensor_replaced", "Sensor was reset or removed")
        with sensor.guard:
            sensor.queues[queue] = loop
        try: self.ensure_subscription(sensor)
        except Exception:
            with sensor.guard: sensor.queues.pop(queue, None)
            raise
        return SensorSubscription(self, sensor, queue)

    def release_subscription(self, sensor):
        with sensor.guard:
            busy = bool(sensor.queues or sensor.observers)
        if not busy and self.subscriptions.get(sensor.topic) is sensor:
            self.world.unsubscribe(sensor.topic)
            self.subscriptions.pop(sensor.topic, None)

    def unsubscribe_queue(self, sensor, queue):
        with sensor.guard: sensor.queues.pop(queue, None)
        self.release_subscription(sensor)

    def unsubscribe_sensor(self, sensor):
        if self.world and self.subscriptions.get(sensor.topic) is sensor:
            self.world.unsubscribe(sensor.topic)
            self.subscriptions.pop(sensor.topic, None)
        with sensor.guard:
            sensor.queues.clear()
            sensor.observers.clear()

    def unsubscribe_all(self):
        for sensor in self.sensors.values():
            with sensor.guard:
                sensor.retired = True
                sensor.close_reason = "Gazebo world restarted"
                sensor.epoch += 1
                sensor.latest = None
            self.unsubscribe_sensor(sensor)
        self.subscriptions.clear()
