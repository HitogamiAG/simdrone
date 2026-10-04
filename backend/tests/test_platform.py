import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app, create_app
from app.errors import BackendError
from app.schemas import DroneCreate, ParameterPatch, Pose, Vector3
from app.service import Platform
from app.realtime import LatestByType


class Gazebo:
    def __init__(self):
        self.items = {}
        self.next_id = 0
        self.state = {"name": "empty", "physics": {"max_step_size": .004, "real_time_factor": 1.0},
                      "gravity": {"x": 0, "y": 0, "z": -9.8},
                      "spherical_coordinates": {"latitude_deg": 1, "longitude_deg": 2,
                                                "elevation": 3, "heading_deg": 0, "surface_model": "EARTH_WGS84"},
                      "simulation": {"paused": False}, "models": []}
        self.calls = []
        self.timeline = None

    async def close(self): pass
    async def world(self): return self.state
    async def drones(self): return list(self.items.values())
    async def create_drone(self, body):
        self.next_id += 1
        value = {"id": f"gz-{self.next_id}", "name": body["name"], "model": body["model"],
                 "pose": body["pose"], "entity_id": self.next_id, "sensors": []}
        self.items[value["id"]] = value
        self.calls.append("create"); self.timeline.append(("gazebo", "create"))
        return value
    async def delete_drone(self, drone_id): self.items.pop(drone_id, None); self.calls.append("delete"); self.timeline.append(("gazebo", "delete"))
    async def reset_drone(self, drone_id): self.calls.append("reset"); self.timeline.append(("gazebo", "reset")); return self.items[drone_id]
    async def sensors(self, drone_id): return [{"id": "imu", "update_rate": 100.0}]
    async def sensor(self, drone_id, sensor_id):
        sensor_type = "camera" if sensor_id == "camera" else "imu"
        return {"id": sensor_id, "type": sensor_type, "update_rate": 100.0, "capabilities": ["update_rate"]}
    async def patch_sensor(self, drone_id, sensor_id, body): self.calls.append(("sensor_patch", body)); self.timeline.append(("gazebo", "sensor_patch")); return {}
    async def reset_sensor(self, drone_id, sensor_id): return {}
    async def world_reset(self): self.calls.append("world_reset"); self.timeline.append(("gazebo", "world_reset")); self.items.clear(); return {"reset": True}
    async def reboot(self): self.calls.append("reboot"); self.timeline.append(("gazebo", "reboot")); self.items.clear(); return {"process_alive": True}
    async def patch_world(self, body): self.calls.append(("world_patch", body)); self.timeline.append(("gazebo", "world_patch")); return {"world": self.state}
    async def pause(self): self.state["simulation"]["paused"] = True; return {"paused": True}
    async def resume(self): self.state["simulation"]["paused"] = False; return {"paused": False}
    async def call(self, method, path, **kwargs): self.calls.append((method, path)); return {"active": path.endswith("activate")}


class Hub:
    def __init__(self): self.items = {}; self.calls = []; self.timeline = None; self.next_id = 0
    async def close(self): pass
    async def instances(self): return [{"id": key, "binding": {"drone_id": value}} for key, value in self.items.items()]
    async def create(self, drone_id):
        self.next_id += 1
        iid = f"px-{self.next_id}"
        self.items[iid] = drone_id
        self.calls.append(("create", drone_id))
        self.timeline.append(("hub", "create"))
        return {"id": iid, "status": "running"}
    async def delete(self, iid): self.items.pop(iid, None); self.calls.append(("delete", iid)); self.timeline.append(("hub", "delete"))
    async def get(self, iid): return {"id": iid, "status": "running"}
    async def start(self, iid): self.calls.append(("start", iid)); return {"id": iid, "status": "running"}
    async def stop(self, iid): self.calls.append(("stop", iid)); return {"id": iid, "status": "stopped"}
    async def restart(self, iid): self.calls.append(("restart", iid)); return {"id": iid, "status": "running"}
    async def parameters(self, iid): return {"MPC_XY_VEL_MAX": 10.0}
    async def patch_parameters(self, iid, body): return {"applied": body}


def platform():
    p = Platform(gazebo=Gazebo(), hub=Hub())
    p.timeline = []
    p.gazebo.timeline = p.hub.timeline = p.timeline
    return p


def test_openapi_hides_pose_patch_and_exposes_platform_api():
    schema = app.openapi()["paths"]
    assert "patch" not in schema["/api/v1/drones/{drone_id}/"]
    for path in ("/api/v1/system/status", "/api/v1/world/reset",
                 "/api/v1/drones/{drone_id}/autopilot/start",
                 "/api/v1/drones/{drone_id}/sensors/{sensor_id}/video"):
        assert path in schema


def test_world_patch_resets_drones_then_applies_merged_settings():
    async def run():
        p = platform()
        item = await p.create_drone(DroneCreate(name="one"))
        previous = item["id"]
        body = {"physics": {"max_step_size": .01}}
        result = await p.patch_world(body)
        assert result["applied"] == ["physics"]
        assert p.gazebo.calls.index("world_reset") < next(i for i, x in enumerate(p.gazebo.calls) if isinstance(x, tuple) and x[0] == "world_patch")
        patch = next(x[1] for x in p.gazebo.calls if isinstance(x, tuple) and x[0] == "world_patch")
        assert patch["physics"] == {"max_step_size": .01, "real_time_factor": 1.0}
        assert not p.drones and not p.hub.items
        with pytest.raises(BackendError) as exc: await p.get_drone(previous)
        assert exc.value.status == 404
    asyncio.run(run())


def test_sensor_patch_resets_px4_before_sensor_change_and_recreates():
    async def run():
        p = platform()
        drone = await p.create_drone(DroneCreate(name="one"))
        item = p._record(drone["id"])
        old_instance = item.instance_id
        await p.patch_sensor(item.id, "imu", {"update_rate": 20})
        assert p.timeline.index(("hub", "delete")) < p.timeline.index(("gazebo", "reset"))
        assert p.gazebo.calls.index("reset") < p.gazebo.calls.index(("sensor_patch", {"update_rate": 20.0}))
        assert item.instance_id != old_instance and item.generation == 2 and item.status == "ready"
        await p.close()
    asyncio.run(run())


def test_world_reset_removes_unmapped_hub_instances_and_all_drones():
    async def run():
        p = platform()
        await p.create_drone(DroneCreate(name="one"))
        p.startup_world_check = await p.ready()
        assert p.startup_world_check["reason"] == "configured_world_contains_drone"
        p.hub.items["orphan"] = "unknown"
        await p.reset_world()
        assert not p.hub.items and not p.gazebo.items and not p.drones
        assert p.world_generation == 1
        assert p.startup_world_check == {"ready": True, "world": "empty"}
        assert (await p.create_drone(DroneCreate(name="after-reset")))["status"] == "ready"
    asyncio.run(run())


def test_creation_compensates_instance_discovered_after_lost_response():
    async def run():
        p = platform()
        async def uncertain(drone_id):
            p.hub.items["created-but-response-lost"] = drone_id
            raise BackendError(504, "timeout", "response timed out")
        p.hub.create = uncertain
        with pytest.raises(BackendError): await p.create_drone(DroneCreate(name="one"))
        assert not p.hub.items and not p.gazebo.items and not p.drones
    asyncio.run(run())


def test_creation_discovers_and_removes_gazebo_model_after_lost_response():
    async def run():
        p = platform()
        create = p.gazebo.create_drone

        async def uncertain(body):
            await create(body)
            raise BackendError(503, "dependency_unavailable", "Gazebo response was lost")

        p.gazebo.create_drone = uncertain
        with pytest.raises(BackendError):
            await p.create_drone(DroneCreate(name="uncertain"))
        assert not p.gazebo.items and not p.hub.items and not p.drones

    asyncio.run(run())


def test_duplicate_model_name_conflict_preserves_existing_model():
    async def run():
        p = platform()
        existing = {"id": "gz-existing", "name": "taken", "model": "x500_gimbal"}
        p.gazebo.items[existing["id"]] = existing
        with pytest.raises(BackendError) as exc:
            await p.create_drone(DroneCreate(name="taken"))
        assert exc.value.code == "drone_name_conflict"
        assert p.gazebo.items == {existing["id"]: existing}
        assert not p.hub.items and not p.drones

    asyncio.run(run())


def test_creation_cleanup_preserves_model_when_px4_presence_is_unknown():
    async def run():
        p = platform()
        async def create_then_lose_response(drone_id):
            p.hub.items["px-orphan"] = drone_id
            raise BackendError(503, "dependency_unavailable", "Hub create response was lost")

        async def failed_delete(_instance_id):
            raise BackendError(503, "dependency_unavailable", "Hub cannot delete instance")

        p.hub.create = create_then_lose_response
        p.hub.delete = failed_delete
        with pytest.raises(BackendError):
            await p.create_drone(DroneCreate(name="preserve-on-unknown"))
        assert len(p.gazebo.items) == 1
        retained = next(iter(p.drones.values()))
        assert retained.status == "failed" and retained.gazebo_id is not None
        assert retained.instance_id == "px-orphan"
        assert retained.last_error["cleanup_failures"]

    asyncio.run(run())


def test_failed_drone_remains_readable_when_upstream_resources_are_missing():
    async def run():
        p = platform()
        created = await p.create_drone(DroneCreate(name="vanished"))
        item = p._record(created["id"])
        item.status = "failed"

        async def missing_gazebo(_drone_id):
            raise BackendError(404, "drone_not_found", "Gazebo model is gone")

        async def unavailable_hub(_instance_id):
            raise BackendError(503, "dependency_unavailable", "PX4 Hub is offline")

        p.gazebo.drone = missing_gazebo
        p.hub.get = unavailable_hub
        response = await p.get_drone(item.id)
        assert response["status"] == "failed"
        assert response["simulation"]["pose"] is None
        assert response["simulation"]["error"]["code"] == "drone_not_found"
        assert response["autopilot"]["error"]["code"] == "dependency_unavailable"

    asyncio.run(run())


def test_camera_actions_share_mutation_lock_and_reject_non_cameras():
    async def run():
        p = platform()
        created = await p.create_drone(DroneCreate(name="camera-lock"))
        item = p._record(created["id"])
        await p.lock.acquire()
        action = asyncio.create_task(p.sensor_action(item.id, "camera", "activate"))
        await asyncio.sleep(0)
        assert not action.done() and not p.gazebo.calls[-1][0] == "POST"
        p.lock.release()
        await action
        assert p.gazebo.calls[-1] == ("POST", f"/api/v1/drones/{item.gazebo_id}/sensors/camera/activate")
        with pytest.raises(BackendError) as exc:
            await p.sensor_action(item.id, "imu", "activate")
        assert exc.value.code == "not_a_camera"

    asyncio.run(run())


def test_validation_errors_are_json_and_follow_api_error_contract():
    p = platform()
    application = create_app(platform_factory=lambda _settings: p)
    with TestClient(application) as client:
        response = client.patch("/api/v1/world", json={"physics": {"unknown": 1}})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_input"
        assert response.json()["error"]["details"]

        response = client.post("/api/v1/drones/", json={
            "pose": {"position": {"x": 0, "y": 0, "z": 1},
                     "orientation": {"x": 0, "y": 0, "z": 0, "w": 0}}
        })
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_input"


def test_pause_rejected_before_creating_any_resources():
    async def run():
        p = platform()
        p.gazebo.state["simulation"]["paused"] = True
        with pytest.raises(BackendError) as exc: await p.create_drone(DroneCreate(name="one"))
        assert exc.value.code == "simulation_paused"
        assert not p.gazebo.items and not p.hub.items and not p.drones
    asyncio.run(run())


def test_realtime_mailbox_keeps_latest_sample_per_type_and_clear_invalidates_old_data():
    queue = LatestByType()
    queue.put_nowait({"type": "position", "value": 1})
    queue.put_nowait({"type": "health", "value": 1})
    queue.put_nowait({"type": "position", "value": 2})
    assert queue.get_nowait() == {"type": "position", "value": 2}
    assert queue.get_nowait() == {"type": "health", "value": 1}
    queue.put_nowait({"type": "position", "value": 3})
    queue.clear()
    queue.put_nowait({"type": "invalidated"})
    assert queue.get_nowait() == {"type": "invalidated"}


def test_autopilot_restart_invalidates_realtime_before_closing_hub_stream():
    async def run():
        p = platform()
        drone = await p.create_drone(DroneCreate(name="stream-owner"))
        events = []
        class Realtime:
            async def invalidate_drone(self, drone_id):
                events.append(("invalidate", drone_id))
        p.realtime = Realtime()
        restart = p.hub.restart
        async def tracked_restart(instance_id):
            events.append(("restart", instance_id))
            return await restart(instance_id)
        p.hub.restart = tracked_restart
        await p.autopilot_action(drone["id"], "restart")
        assert events == [("invalidate", drone["id"]), ("restart", drone["autopilot"]["id"])]
        assert p.drones[drone["id"]].status == "ready"
    asyncio.run(run())
