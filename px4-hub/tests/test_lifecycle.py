"""Lifecycle with fake adapters, not a real PX4/Gazebo integration."""
import asyncio

import pytest

from app.config import Settings
from app.errors import HubError
from app.service import InstanceService
from app.telemetry import TelemetryFanout


class Process:
    pid = 123
    returncode = None

    def __init__(self):
        self.exited = asyncio.Event()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def exit(self):
        self.returncode = 1
        self.exited.set()


class Gazebo:
    paused = False

    async def simulation(self):
        await asyncio.sleep(0)
        return 'empty', self.paused

    async def drone(self, drone_id):
        await asyncio.sleep(0)
        return {'name': drone_id, 'model': 'x500_gimbal', 'entity_id': hash(drone_id)}

    async def close(self):
        pass


class Adapter:
    fail = False

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.stopped = []

    async def start(self, record):
        record.workdir.mkdir(parents=True, exist_ok=True)
        record.system = object()
        record.px4_process, record.mavsdk_process = Process(), Process()
        record.telemetry = TelemetryFanout(record.system, record.id)
        if self.fail:
            raise RuntimeError('test startup failure')

    async def stop(self, record):
        self.stopped.append(record.id)
        if record.telemetry:
            await record.telemetry.close()
        for process in (record.px4_process, record.mavsdk_process):
            if process:
                process.exit()
        record.telemetry = record.system = None
        record.px4_process = record.mavsdk_process = None

    async def set_parameter(self, record, name, value):
        self.entered.set()
        await self.release.wait()
        return value

    async def get_parameters(self, record):
        return {'MPC_XY_VEL_MAX': 10.0}


def service(tmp_path):
    return InstanceService(Settings(instance_dir=tmp_path), Gazebo(), Adapter())


def test_concurrent_slots_limit_duplicate_and_reuse(tmp_path):
    async def run():
        hub = service(tmp_path)
        results = await asyncio.gather(*(hub.create(f'd{i}', 'x500_gimbal') for i in range(4)), return_exceptions=True)
        assert len(hub.instances) == 3
        assert {r.slot for r in hub.instances.values()} == {0, 1, 2}
        assert sum(isinstance(r, HubError) and r.code == 'instance_limit' for r in results) == 1
        with pytest.raises(HubError) as exc:
            await hub.create('d0', 'x500_gimbal')
        assert exc.value.code == 'drone_already_bound'
        first = results[0]['id']
        await hub.delete(first)
        assert not (tmp_path / first).exists()
        assert (await hub.create('replacement', 'x500_gimbal'))['process']['instance_id'] == 0
        await hub.shutdown()
        assert not list(tmp_path.iterdir())
    asyncio.run(run())


def test_start_failure_and_failed_restart_release_processes(tmp_path):
    async def run():
        hub = service(tmp_path)
        hub.px4.fail = True
        with pytest.raises(HubError):
            await hub.create('d0', 'x500_gimbal')
        assert not hub.instances and not list(tmp_path.iterdir())
        hub.px4.fail = False
        result = await hub.create('d0', 'x500_gimbal')
        record = hub.instances[result['id']]
        hub.px4.fail = True
        with pytest.raises(HubError):
            await hub.restart(record.id)
        assert record.status == 'failed' and record.system is None
        assert record.workdir.exists()
        hub.px4.fail = False
        assert (await hub.restart(record.id))['status'] == 'running'
        await hub.shutdown()
    asyncio.run(run())


def test_stop_start_preserves_instance_slot_and_parameter_directory(tmp_path):
    async def run():
        hub = service(tmp_path)
        created = await hub.create('d0', 'x500_gimbal')
        record = hub.instances[created['id']]
        marker = record.workdir / 'parameters.marker'
        marker.write_text('persist')
        stopped = await hub.stop(record.id)
        assert stopped['status'] == 'stopped'
        assert record.system is None and record.px4_process is None
        assert marker.read_text() == 'persist'
        assert (await hub.stop(record.id))['status'] == 'stopped'
        started = await hub.start_instance(record.id)
        assert started['id'] == created['id'] and started['status'] == 'running'
        assert marker.read_text() == 'persist'
        assert (await hub.start_instance(record.id))['status'] == 'running'
        await hub.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['delete', 'restart'])
def test_parameter_change_serialized_with_lifecycle(tmp_path, operation):
    async def run():
        hub = service(tmp_path)
        record_id = (await hub.create('d0', 'x500_gimbal'))['id']
        hub.px4.release.clear()
        patch = asyncio.create_task(hub.patch_parameters(record_id, {'MPC_XY_VEL_MAX': 10.0}))
        await hub.px4.entered.wait()
        lifecycle = asyncio.create_task(getattr(hub, operation)(record_id))
        await asyncio.sleep(0.01)
        assert not lifecycle.done() and not hub.px4.stopped
        hub.px4.release.set()
        assert (await patch)['applied']['MPC_XY_VEL_MAX'] == 10.0
        await lifecycle
        await hub.shutdown()
    asyncio.run(run())


def test_process_failure_stops_pair_and_preserves_neighbour(tmp_path):
    async def run():
        hub = service(tmp_path)
        first = (await hub.create('d0', 'x500_gimbal'))['id']
        second = (await hub.create('d1', 'x500_gimbal'))['id']
        hub.instances[first].mavsdk_process.exit()
        await asyncio.wait_for(hub.instances[first].monitor, 1)
        assert hub.instances[first].status == 'failed'
        assert hub.instances[first].px4_process is None
        assert hub.instances[second].status == 'running'
        await hub.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('timeout', [False, True])
def test_interrupted_start_releases_slot_and_directory(tmp_path, timeout):
    async def run():
        from dataclasses import replace
        hub = service(tmp_path)
        hub.settings = replace(hub.settings, startup_timeout=0.05)
        original = hub.px4.start
        entered = asyncio.Event()
        async def blocked(record):
            await original(record)
            entered.set()
            await asyncio.Event().wait()
        hub.px4.start = blocked
        creating = asyncio.create_task(hub.create('d0', 'x500_gimbal'))
        await entered.wait()
        if timeout:
            with pytest.raises(HubError) as exc:
                await creating
            assert exc.value.code == 'startup_timeout'
        else:
            creating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await creating
        assert not hub.instances and not list(tmp_path.iterdir())
        hub.px4.start = original
        assert (await hub.create('d0', 'x500_gimbal'))['process']['instance_id'] == 0
        await hub.shutdown()
    asyncio.run(run())


def test_parameter_partial_failure_reports_confirmed_fields(tmp_path):
    async def run():
        hub = service(tmp_path)
        instance_id = (await hub.create('d0', 'x500_gimbal'))['id']
        async def set_parameter(record, name, value):
            if name == 'MPC_Z_VEL_MAX_UP':
                raise TimeoutError('unconfirmed')
            return value
        hub.px4.set_parameter = set_parameter
        with pytest.raises(HubError) as exc:
            await hub.patch_parameters(instance_id, {'MPC_XY_VEL_MAX': 10, 'MPC_Z_VEL_MAX_UP': 2})
        assert exc.value.details['applied'] == {'MPC_XY_VEL_MAX': 10}
        assert set(exc.value.details['unconfirmed']) == {'MPC_Z_VEL_MAX_UP'}
        await hub.shutdown()
    asyncio.run(run())
