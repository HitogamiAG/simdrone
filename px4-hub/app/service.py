import asyncio
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import Px4Adapter
from .errors import HubError
from .gazebo import GazeboApi


@dataclass
class Instance:
    id: str
    drone_id: str
    gazebo_model: str
    entity_id: int | None
    slot: int
    workdir: Path
    status: str = "starting"
    last_error: str | None = None
    px4_process: asyncio.subprocess.Process | None = None
    mavsdk_process: asyncio.subprocess.Process | None = None
    system: object | None = None
    telemetry: object | None = None
    monitor: asyncio.Task | None = None
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    udp_port: int = field(init=False)
    grpc_port: int = field(init=False)

    def __post_init__(self):
        self.udp_port = 14540 + self.slot
        self.grpc_port = 50051 + self.slot


class InstanceService:
    parameter_names = ("MPC_XY_VEL_MAX", "MPC_Z_VEL_MAX_UP", "MPC_Z_VEL_MAX_DN")

    def __init__(self, settings):
        self.settings = settings
        self.gazebo = GazeboApi(settings.gazebo_api_url)
        self.px4 = Px4Adapter(settings)
        self.instances: dict[str, Instance] = {}
        self.lock = asyncio.Lock()
        self.poller: asyncio.Task | None = None
        self.gazebo_available = False
        self.simulation_paused: bool | None = None
        self.world_name: str | None = None
        self.closed = False

    async def start(self):
        self.settings.instance_dir.mkdir(parents=True, exist_ok=True)
        self.poller = asyncio.create_task(self._poll_gazebo())

    async def shutdown(self):
        self.closed = True
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        async with self.lock:
            records = list(self.instances.values())
            self.instances.clear()
        await asyncio.gather(*(self._dispose(item, remove_dir=True) for item in records), return_exceptions=True)
        await self.gazebo.close()

    async def _poll_gazebo(self):
        while not self.closed:
            try:
                self.world_name, self.simulation_paused = await self.gazebo.simulation()
                self.gazebo_available = True
                for record in list(self.instances.values()):
                    try:
                        drone = await self.gazebo.drone(record.drone_id)
                        if drone.get("entity_id") != record.entity_id:
                            record.last_error = "Gazebo entity changed; backend coordination is required"
                    except HubError:
                        record.last_error = "Gazebo model is missing; backend coordination is required"
            except Exception:
                self.gazebo_available = False
                self.simulation_paused = None
            await asyncio.sleep(self.settings.poll_interval)

    async def list(self):
        async with self.lock:
            records = list(self.instances.values())
        return [self.serialize(item) for item in records]

    async def create(self, drone_id: str, profile: str):
        if profile != "x500_gimbal":
            raise HubError(422, "unsupported_profile", "Only x500_gimbal is supported")
        await self._require_running_world()
        drone = await self.gazebo.drone(drone_id)
        if drone.get("model") != "x500_gimbal":
            raise HubError(422, "incompatible_drone", "PX4 profile requires a Gazebo x500_gimbal", {"model": drone.get("model")})
        async with self.lock:
            if any(item.drone_id == drone_id for item in self.instances.values()):
                raise HubError(409, "drone_already_bound", "Gazebo drone already has a PX4 instance")
            occupied = {item.slot for item in self.instances.values()}
            slot = next((value for value in range(self.settings.max_instances) if value not in occupied), None)
            if slot is None:
                raise HubError(409, "instance_limit", "PX4 instance limit reached", {"limit": self.settings.max_instances})
            instance_id = str(uuid.uuid4())
            workdir = self.settings.instance_dir / instance_id
            record = Instance(instance_id, drone_id, drone["name"], drone.get("entity_id"), slot, workdir)
            self.instances[instance_id] = record
        try:
            await self._start_record(record)
        except Exception as exc:
            record.status = "failed"
            record.last_error = str(exc)
            await self._dispose(record, remove_dir=True)
            async with self.lock:
                self.instances.pop(record.id, None)
            if isinstance(exc, TimeoutError):
                raise HubError(504, "startup_timeout", str(exc)) from exc
            raise HubError(503, "instance_start_failed", "PX4 instance failed to start", str(exc)) from exc
        return self.serialize(record)

    async def get(self, instance_id):
        return self.serialize(self._get(instance_id))

    async def restart(self, instance_id):
        record = self._get(instance_id)
        async with record.operation_lock:
            if self.instances.get(instance_id) is not record or record.status == "stopping":
                raise HubError(409, "instance_stopping", "PX4 instance is being deleted")
            await self._require_running_world()
            record.status = "restarting"
            if record.monitor:
                record.monitor.cancel()
                await asyncio.gather(record.monitor, return_exceptions=True)
                record.monitor = None
            await self.px4.stop(record)
            try:
                await self._start_record(record)
            except Exception as exc:
                record.status, record.last_error = "failed", str(exc)
                raise HubError(503, "restart_failed", "PX4 instance failed to restart", str(exc)) from exc
        return self.serialize(record)

    async def delete(self, instance_id):
        record = self._get(instance_id)
        async with record.operation_lock:
            record.status = "stopping"
            if record.monitor:
                record.monitor.cancel()
                await asyncio.gather(record.monitor, return_exceptions=True)
                record.monitor = None
            await self._dispose(record, remove_dir=True)
            async with self.lock:
                self.instances.pop(instance_id, None)
        return {"deleted": True, "id": instance_id, "gazebo_drone_preserved": True}

    async def get_parameters(self, instance_id):
        record = self._get_running(instance_id)
        try:
            return await self.px4.get_parameters(record)
        except Exception as exc:
            raise HubError(503, "parameter_read_failed", "Could not read PX4 parameters", str(exc)) from exc

    async def patch_parameters(self, instance_id, values):
        record = self._get_running(instance_id)
        await self._require_running_world()
        requested = {key: value for key, value in values.items() if value is not None}
        if not requested:
            raise HubError(422, "empty_patch", "At least one supported parameter is required")
        applied, unconfirmed = {}, {}
        for name, value in requested.items():
            try:
                applied[name] = await self.px4.set_parameter(record, name, value)
            except Exception as exc:
                unconfirmed[name] = str(exc)
        if unconfirmed:
            raise HubError(503, "parameter_patch_partial", "Some PX4 parameters were not confirmed", {"applied": applied, "unconfirmed": unconfirmed})
        return {"applied": applied, "parameters": await self.get_parameters(instance_id)}

    def _get(self, instance_id):
        record = self.instances.get(instance_id)
        if not record:
            raise HubError(404, "instance_not_found", "PX4 instance does not exist")
        return record

    def _get_running(self, instance_id):
        record = self._get(instance_id)
        if record.status != "running" or not record.system:
            raise HubError(409, "instance_not_running", "PX4 instance is not running", {"status": record.status})
        return record

    async def _require_running_world(self):
        try:
            self.world_name, paused = await self.gazebo.simulation()
        except HubError:
            raise
        if paused:
            raise HubError(409, "simulation_paused", "PX4 startup and parameter changes require a running simulation")
        self.gazebo_available, self.simulation_paused = True, False

    async def _start_record(self, record):
        record.status, record.last_error = "starting", None
        try:
            await asyncio.wait_for(self.px4.start(record), self.settings.startup_timeout)
        except Exception:
            await self.px4.stop(record)
            raise
        record.status = "running"
        record.monitor = asyncio.create_task(self._watch(record))

    async def _watch(self, record):
        waits = [asyncio.create_task(record.px4_process.wait()), asyncio.create_task(record.mavsdk_process.wait())]
        try:
            done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if record.status in ("stopping", "restarting") or self.closed:
                return
            process = "PX4" if waits[0] in done else "MAVSDK"
            record.status, record.last_error = "failed", f"{process} process exited unexpectedly"
            for task in pending:
                task.cancel()
            await self.px4.stop(record)
        except asyncio.CancelledError:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
            raise

    async def _dispose(self, record, remove_dir):
        record.status = "stopping"
        if record.monitor and record.monitor is not asyncio.current_task():
            record.monitor.cancel()
            await asyncio.gather(record.monitor, return_exceptions=True)
        await self.px4.stop(record)
        if remove_dir:
            await asyncio.to_thread(shutil.rmtree, record.workdir, True)
        record.status = "failed" if record.last_error else "stopping"

    def serialize(self, record):
        now = asyncio.get_running_loop().time()
        latest = record.telemetry.latest if record.telemetry else {}
        health = latest.get("health", {})
        fresh = bool(record.telemetry and record.telemetry.last_received is not None and now - record.telemetry.last_received <= self.settings.telemetry_stale_after)
        process_alive = bool(record.px4_process and record.px4_process.returncode is None and record.mavsdk_process and record.mavsdk_process.returncode is None)
        return {
            "id": record.id, "status": record.status,
            "process": {"px4_pid": record.px4_process.pid if record.px4_process else None,
                        "mavsdk_pid": record.mavsdk_process.pid if record.mavsdk_process else None,
                        "instance_id": record.slot, "alive": process_alive},
            "binding": {"world": self.world_name, "drone_id": record.drone_id,
                        "gazebo_model": record.gazebo_model, "entity_id": record.entity_id,
                        "valid": record.last_error is None},
            "px4": {"system_id": record.slot + 1, "airframe": 4019,
                    "connected": bool(record.system and fresh), "telemetry_fresh": fresh,
                    "health": health,
                    "ready_to_arm": bool(health and health.get("is_global_position_ok") and health.get("is_home_position_ok"))},
            "mavlink": {"connected": bool(record.system and fresh), "udp_port": record.udp_port,
                         "grpc_port": record.grpc_port},
            "dependencies": {"gazebo_available": self.gazebo_available,
                             "simulation_paused": self.simulation_paused},
            "last_error": record.last_error,
            "capabilities": {"parameters": True, "telemetry": True, "flight": False,
                             "missions": False, "control": False},
        }

    async def close_stream(self, record):
        return
