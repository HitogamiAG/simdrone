from fastapi.testclient import TestClient
from fastapi import WebSocketDisconnect
from types import SimpleNamespace

from app.api import create_app
from app.config import Settings
from app.schemas import InstanceCreate, ParameterPatch
from app.telemetry import TelemetryFanout
from app.processes import prepare_rootfs


class FakeService:
    def __init__(self, settings):
        self.settings = settings

    async def start(self):
        pass

    async def shutdown(self):
        pass

    async def list(self):
        return []

    async def create(self, drone_id, profile):
        return {"id": "fake-id", "drone_id": drone_id, "profile": profile}

    async def get(self, instance_id):
        return {"id": instance_id}

    async def delete(self, instance_id):
        return {"deleted": True, "id": instance_id}

    async def restart(self, instance_id):
        return {"id": instance_id, "status": "running"}

    async def get_parameters(self, instance_id):
        return {"MPC_XY_VEL_MAX": 12.0}

    async def patch_parameters(self, instance_id, values):
        return {"applied": values}


def test_instance_lifecycle_routes_and_errors():
    with TestClient(create_app(Settings(), FakeService)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/v1/instances/").json() == []
        created = client.post("/api/v1/instances/", json={"drone_id": "drone-a"})
        assert created.status_code == 201
        assert created.json()["profile"] == "x500_gimbal"
        assert client.get("/api/v1/instances/fake-id/").status_code == 200
        assert client.post("/api/v1/instances/fake-id/restart").json()["status"] == "running"
        assert client.get("/api/v1/instances/fake-id/parameters/").status_code == 200
        assert client.patch("/api/v1/instances/fake-id/parameters/", json={"MPC_XY_VEL_MAX": 8}).json()["applied"] == {"MPC_XY_VEL_MAX": 8.0}
        assert client.delete("/api/v1/instances/fake-id/").status_code == 200
        bad = client.post("/api/v1/instances/", json={"drone_id": "drone-a", "world": "other"})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_input"
        forbidden = client.patch("/api/v1/instances/fake-id/parameters/", json={"MAV_SYS_ID": 8})
        assert forbidden.status_code == 422


def test_request_schemas_restrict_profile_and_parameters():
    assert InstanceCreate(drone_id="drone-a").profile == "x500_gimbal"
    for payload in ({"drone_id": "drone-a", "profile": "custom"}, {"drone_id": "drone-a", "path": "/tmp/model.sdf"}):
        try:
            InstanceCreate.model_validate(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported instance field was accepted")
    for payload in ({"MPC_XY_VEL_MAX": 0}, {"MPC_XY_VEL_MAX": float("inf")}, {"MAV_SYS_ID": 4}):
        try:
            ParameterPatch.model_validate(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported parameter was accepted")


def test_telemetry_fanout_is_per_client_and_bounded():
    import asyncio

    async def run():
        fanout = TelemetryFanout(object(), "i1")
        first, second = fanout.subscribe(), fanout.subscribe()
        for value in range(12):
            message = {"instance_id": "i1", "type": "position", "data": {"x": value}}
            for queue in tuple(fanout.clients):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(message)
        assert first.qsize() == second.qsize() == len(__import__("app.telemetry", fromlist=["STREAMS"]).STREAMS)
        assert (await first.get())["data"]["x"] == 3
        assert (await second.get())["data"]["x"] == 3
        fanout.unsubscribe(first)
        assert len(fanout.clients) == 1

    asyncio.run(run())


def test_prepare_rootfs_accepts_precreated_instance_directory(tmp_path):
    template = tmp_path / "template"
    target = tmp_path / "instance"
    template.mkdir()
    target.mkdir()
    (template / "etc").mkdir()
    (template / "etc" / "rcS").write_text("startup")

    prepare_rootfs(template, target)

    assert (target / "etc" / "rcS").read_text() == "startup"


def test_telemetry_websocket_unsubscribes_when_instance_is_deleted():
    import asyncio

    class Fanout:
        def __init__(self):
            self.clients = set()

        def subscribe(self):
            queue = asyncio.Queue(maxsize=1)
            self.clients.add(queue)
            return queue

        def unsubscribe(self, queue):
            self.clients.discard(queue)

    service = FakeService(Settings())
    fanout = Fanout()
    record = SimpleNamespace(status="running", telemetry=fanout)
    service._get_running = lambda instance_id: record

    with TestClient(create_app(Settings(), lambda _: service)) as client:
        with client.websocket_connect("/api/v1/instances/instance-a/telemetry") as socket:
            assert len(fanout.clients) == 1
            record.status = "stopping"
            record.telemetry = None
            try:
                socket.receive_json()
            except WebSocketDisconnect as exc:
                assert exc.code == 1012
            else:
                raise AssertionError("WebSocket stayed open after instance deletion")

    assert not fanout.clients
