"""Pure response formatters shared by HTTP handlers and domain services."""
import os


def drone_json(record, sensors, sensor_presenter):
    return {"id": record.id, "entity_id": record.entity_id, "name": record.name,
            "model": record.model, "pose": record.pose.model_dump(),
            "pose_source": "Gazebo pose/info", "sensors": [sensor_presenter(item) for item in sensors]}


def sensor_json(sensor, *, capabilities, active=None, stream_url=None):
    return {"id": sensor.name, "type": sensor.kind, "topic": sensor.topic,
            "update_rate": sensor.rate,
            "update_rate_source": "command confirmed by simulation timestamps" if sensor.rate_confirmed else "initial SDF",
            "observed_update_rate": sensor.observed_rate, "capabilities": capabilities,
            "latest": sensor.latest, "stream_url": stream_url, "active": active}
