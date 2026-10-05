"""Lifecycle with fake adapters, not a real PX4/Gazebo integration."""
import asyncio

import pytest

from app.config import Settings
from app.errors import HubError
from app.service import InstanceService
from app.telemetry import TelemetryFanout
from app.telemetry import to_json


def test_unavailable_px4_measurements_remain_valid_browser_json():
    import json
    from types import SimpleNamespace
    sample = to_json(SimpleNamespace(temperature_degc=float("nan"),
                                    voltage_v=float("inf"), remaining_percent=.75,
                                    cells=[float("-inf"), 4.1]))
    decoded = json.loads(json.dumps(sample, allow_nan=False))
    assert decoded == {"temperature_degc": None, "voltage_v": None,
                       "remaining_percent": .75, "cells": [None, 4.1]}


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

    async def world(self):
        return {'name': 'empty', 'spherical_coordinates': {
            'latitude_deg': 0.0, 'longitude_deg': 0.0, 'heading_deg': 0.0,
            'elevation': 0.0, 'surface_model': 'EARTH_WGS84'}}

    async def drone(self, drone_id):
        await asyncio.sleep(0)
        return {'name': drone_id, 'model': 'x500_gimbal', 'entity_id': hash(drone_id),
                'pose': {'position': {'x': 0.0, 'y': 0.0, 'z': 0.0}}}

    async def close(self):
        pass


class Adapter:
    fail = False

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.stopped = []
        self.parameters = {'MPC_XY_VEL_MAX': 10.0}

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
        self.parameters[name] = value
        return value

    async def get_parameters(self, record):
        return dict(self.parameters)


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


def test_stop_start_and_restart_preserve_confirmed_parameters(tmp_path):
    async def run():
        hub = service(tmp_path)
        created = await hub.create('drone', 'x500_gimbal')
        iid = created['id']
        await hub.patch_parameters(iid, {'MPC_XY_VEL_MAX': 7.5})
        await hub.stop(iid)
        assert (await hub.start_instance(iid))['status'] == 'running'
        assert (await hub.get_parameters(iid))['MPC_XY_VEL_MAX'] == 7.5
        await hub.restart(iid)
        assert (await hub.get_parameters(iid))['MPC_XY_VEL_MAX'] == 7.5
        await hub.shutdown()
    asyncio.run(run())


def test_restart_snapshot_failure_keeps_process_pair_monitored(tmp_path):
    async def run():
        hub = service(tmp_path)
        instance_id = (await hub.create('d0', 'x500_gimbal'))['id']
        record = hub.instances[instance_id]
        original_monitor = record.monitor

        async def fail_snapshot(_record):
            raise RuntimeError('MAVSDK parameter read failed')

        hub.px4.get_parameters = fail_snapshot
        with pytest.raises(HubError) as exc:
            await hub.restart(instance_id)
        assert exc.value.code == 'parameter_snapshot_failed'
        assert record.status == 'running'
        assert record.monitor is original_monitor and not original_monitor.done()

        record.px4_process.exit()
        await asyncio.wait_for(original_monitor, 1)
        assert record.status == 'failed'
        assert record.px4_process is None and record.mavsdk_process is None
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


def test_mavsdk_failure_during_armed_flight_preserves_px4_for_failsafe(tmp_path):
    class ActiveFlight:
        class Session:
            armed = True
        session = Session()

        def __init__(self):
            self.lost = False

        async def transport_lost(self):
            self.lost = True

        async def close(self):
            pass

        def state(self):
            return {"active": None, "ready": False}

    async def run():
        hub = service(tmp_path)
        result = await hub.create('d0', 'x500_gimbal')
        record = hub.instances[result['id']]
        flight = ActiveFlight()
        record.flight = flight
        px4_process = record.px4_process
        record.mavsdk_process.exit()
        await asyncio.wait_for(record.monitor, 1)
        assert record.status == 'failed'
        assert record.last_error == 'MAVSDK process exited unexpectedly'
        assert record.px4_process is px4_process and px4_process.returncode is None
        assert flight.lost
        assert not hub.px4.stopped
        process = hub.serialize(record)["process"]
        assert process["px4_alive"] is True and process["mavsdk_alive"] is False
        assert process["alive"] is False
        with pytest.raises(HubError) as exc:
            await hub.restart(record.id)
        assert exc.value.code == 'px4_autonomous_failsafe_active'
        with pytest.raises(HubError) as exc:
            await hub.start_instance(record.id)
        assert exc.value.code == 'px4_autonomous_failsafe_active'
        assert record.px4_process is px4_process and px4_process.returncode is None
        await hub.delete(record.id)
        assert px4_process.returncode is not None
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
