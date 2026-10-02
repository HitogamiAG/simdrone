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
    world: str
    workdir: Path
    status: str = "starting"
    last_error: str | None = None
    binding_valid: bool | None = True
    binding_error: str | None = None
    px4_process: asyncio.subprocess.Process | None = None
    mavsdk_process: asyncio.subprocess.Process | None = None
    system: object | None = None
    telemetry: object | None = None
    saved_parameters: dict | None = None
    monitor: asyncio.Task | None = None
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    udp_port: int = field(init=False)
    grpc_port: int = field(init=False)

    def __post_init__(self):
        self.udp_port = 14540 + self.slot
        self.grpc_port = 50051 + self.slot


class InstanceService:
    parameter_names = ("MPC_XY_VEL_MAX", "MPC_Z_VEL_MAX_UP", "MPC_Z_VEL_MAX_DN")

    def __init__(self, settings, gazebo=None, px4=None):
        self.settings = settings
        self.gazebo = gazebo if gazebo is not None else GazeboApi(settings.gazebo_api_url)
        self.px4 = px4 if px4 is not None else Px4Adapter(settings)
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
        await asyncio.gather(*(self._shutdown_record(item) for item in records))
        await self.gazebo.close()

    async def _shutdown_record(self, record):
        async with record.operation_lock:
            await self._dispose(record, remove_dir=True)

    async def _poll_gazebo(self):
        while not self.closed:
            await self._refresh_gazebo()
            await asyncio.sleep(self.settings.poll_interval)

    async def _refresh_gazebo(self):
        try:
            self.world_name, self.simulation_paused = await self.gazebo.simulation()
            self.gazebo_available = True
        except Exception:
            self.gazebo_available = False
            self.simulation_paused = None
            for record in list(self.instances.values()):
                record.binding_valid = None
                record.binding_error = "Gazebo unavailable; binding cannot be observed"
            return
        for record in list(self.instances.values()):
            try:
                drone = await self.gazebo.drone(record.drone_id)
                valid = (self.world_name == record.world and drone.get("entity_id") == record.entity_id
                         and drone.get("name") == record.gazebo_model and drone.get("model") == "x500_gimbal")
                record.binding_valid = valid
                record.binding_error = None if valid else "Gazebo binding changed; backend coordination is required"
            except HubError as exc:
                record.binding_valid = False if exc.status == 404 else None
                record.binding_error = ("Gazebo model is missing; backend coordination is required"
                                        if exc.status == 404 else "Gazebo binding cannot be observed")

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
            if self.closed:
                raise HubError(503, "hub_stopping", "Hub is shutting down")
            if any(item.drone_id == drone_id for item in self.instances.values()):
                raise HubError(409, "drone_already_bound", "Gazebo drone already has a PX4 instance")
            occupied = {item.slot for item in self.instances.values()}
            slot = next((value for value in range(self.settings.max_instances) if value not in occupied), None)
            if slot is None:
                raise HubError(409, "instance_limit", "PX4 instance limit reached", {"limit": self.settings.max_instances})
            instance_id = str(uuid.uuid4())
            workdir = self.settings.instance_dir / instance_id
            record = Instance(instance_id, drone_id, drone["name"], drone.get("entity_id"), slot, self.world_name, workdir)
            self.instances[instance_id] = record
        async with record.operation_lock:
            try:
                if self.closed:
                    raise HubError(503, "hub_stopping", "Hub is shutting down")
                await self._start_record(record)
            except BaseException as exc:
                record.status = "failed"
                record.last_error = str(exc)
                await self._dispose(record, remove_dir=True)
                async with self.lock:
                    self.instances.pop(record.id, None)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, HubError):
                    raise
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
            # Keep the process monitor active until the snapshot succeeds. If
            # MAVSDK cannot read parameters, restart must leave the live pair
            # monitored and the record in its original running state.
            await self._capture_parameters(record)
            record.status = "restarting"
            if record.monitor:
                record.monitor.cancel()
                await asyncio.gather(record.monitor, return_exceptions=True)
                record.monitor = None
            try:
                await self.px4.stop(record)
                await self._start_record(record)
            except Exception as exc:
                try:
                    await self.px4.stop(record)
                except Exception:
                    pass
                record.status, record.last_error = "failed", str(exc)
                raise HubError(503, "restart_failed", "PX4 instance failed to restart", str(exc)) from exc
        return self.serialize(record)

    async def stop(self, instance_id):
        record = self._get(instance_id)
        async with record.operation_lock:
            if self.instances.get(instance_id) is not record:
                raise HubError(404, "instance_not_found", "PX4 instance does not exist")
            if record.status == "stopped":
                return self.serialize(record)
            if record.monitor:
                record.monitor.cancel()
                await asyncio.gather(record.monitor, return_exceptions=True)
                record.monitor = None
            await self._capture_parameters(record)
            await self.px4.stop(record)
            record.status, record.last_error = "stopped", None
        return self.serialize(record)

    async def start_instance(self, instance_id):
        record = self._get(instance_id)
        async with record.operation_lock:
            if self.instances.get(instance_id) is not record:
                raise HubError(404, "instance_not_found", "PX4 instance does not exist")
            if record.status == "running":
                return self.serialize(record)
            if record.status not in {"stopped", "failed"}:
                raise HubError(409, "instance_not_startable", "PX4 instance cannot be started", {"status": record.status})
            await self._require_running_world()
            try:
                await self._start_record(record)
            except Exception as exc:
                record.status, record.last_error = "failed", str(exc)
                raise HubError(503, "start_failed", "PX4 instance failed to start", str(exc)) from exc
        return self.serialize(record)

    async def delete(self, instance_id):
        record = self._get(instance_id)
        async with record.operation_lock:
            if self.instances.get(instance_id) is not record:
                raise HubError(404, "instance_not_found", "PX4 instance does not exist")
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
        record = self._get(instance_id)
        async with record.operation_lock:
            self._get_running(instance_id)
            try:
                parameters = await self.px4.get_parameters(record)
                record.saved_parameters = dict(parameters)
                return parameters
            except Exception as exc:
                raise HubError(503, "parameter_read_failed", "Could not read PX4 parameters", str(exc)) from exc

    async def patch_parameters(self, instance_id, values):
        record = self._get(instance_id)
        async with record.operation_lock:
            self._get_running(instance_id)
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
            try:
                parameters = await self.px4.get_parameters(record)
            except Exception as exc:
                raise HubError(503, "parameter_patch_partial", "Changes confirmed but final snapshot unavailable", {"applied": applied, "unconfirmed": {}, "snapshot_error": str(exc)}) from exc
            record.saved_parameters = dict(parameters)
            return {"applied": applied, "parameters": parameters}

    async def _capture_parameters(self, record):
        if record.system is not None and record.status in {"running", "restarting"}:
            try:
                record.saved_parameters = dict(await self.px4.get_parameters(record))
            except Exception as exc:
                raise HubError(503, "parameter_snapshot_failed", "Could not preserve PX4 parameters before stopping", str(exc)) from exc

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
            self.gazebo_available, self.simulation_paused = False, None
            raise
        self.gazebo_available, self.simulation_paused = True, paused
        if paused:
            raise HubError(409, "simulation_paused", "PX4 startup and parameter changes require a running simulation")

    async def _start_record(self, record):
        record.status, record.last_error = "starting", None
        try:
            await asyncio.wait_for(self.px4.start(record), self.settings.startup_timeout)
            if record.saved_parameters is None:
                record.saved_parameters = dict(await self.px4.get_parameters(record))
            else:
                for name, value in record.saved_parameters.items():
                    await self.px4.set_parameter(record, name, value)
                observed = await self.px4.get_parameters(record)
                mismatched = {name: {"expected": value, "observed": observed.get(name)}
                              for name, value in record.saved_parameters.items()
                              if observed.get(name) is None or abs(observed[name] - value) > max(1e-4, abs(value) * 1e-4)}
                if mismatched:
                    raise RuntimeError(f"Saved PX4 parameters were not restored: {mismatched}")
                record.saved_parameters = dict(observed)
        except BaseException:
            await self.px4.stop(record)
            raise
        record.status = "running"
        record.monitor = asyncio.create_task(self._watch(record))

    async def _watch(self, record):
        waits = [asyncio.create_task(record.px4_process.wait()), asyncio.create_task(record.mavsdk_process.wait())]
        try:
            done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            async with record.operation_lock:
                if record.status != "running" or self.closed:
                    return
                process = "PX4" if waits[0] in done else "MAVSDK"
                record.status, record.last_error = "failed", f"{process} process exited unexpectedly"
                await self.px4.stop(record)
        finally:
            for task in waits:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

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
        ages = {name: max(0.0, now - received) for name, received in
                (record.telemetry.received_by_type.items() if record.telemetry else [])}
        freshness = {name: age <= self.settings.telemetry_stale_after for name, age in ages.items()}
        fresh = freshness.get("position", False)
        connected = record.telemetry.connected if record.telemetry else False
        process_alive = bool(record.px4_process and record.px4_process.returncode is None and record.mavsdk_process and record.mavsdk_process.returncode is None)
        return {
            "id": record.id, "status": record.status,
            "process": {"px4_pid": record.px4_process.pid if record.px4_process else None,
                        "mavsdk_pid": record.mavsdk_process.pid if record.mavsdk_process else None,
                        "instance_id": record.slot, "alive": process_alive},
            "binding": {"world": record.world, "drone_id": record.drone_id,
                        "gazebo_model": record.gazebo_model, "entity_id": record.entity_id,
                        "valid": record.binding_valid, "error": record.binding_error},
            "px4": {"system_id": record.slot + 1, "airframe": 4019,
                    "connected": connected, "telemetry_fresh": fresh,
                    "health": health,
                    "telemetry_age_seconds": ages, "telemetry_fresh_by_type": freshness,
                    "stream_errors": record.telemetry.stream_errors if record.telemetry else {},
                    "ready_to_arm": bool(connected and freshness.get("health", False) and health.get("is_armable"))},
            "mavlink": {"connected": connected, "udp_port": record.udp_port,
                         "grpc_port": record.grpc_port},
            "dependencies": {"gazebo_available": self.gazebo_available,
                             "simulation_paused": self.simulation_paused},
            "last_error": record.last_error,
            "capabilities": {"parameters": True, "telemetry": True, "flight": False,
                             "missions": False, "control": False},
        }
