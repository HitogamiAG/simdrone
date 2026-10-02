import asyncio
from types import SimpleNamespace

from app.errors import HubError
from app.telemetry import TelemetryFanout
from test_lifecycle import service


def test_connection_and_position_freshness_are_independent(tmp_path):
    async def run():
        hub = service(tmp_path)
        instance_id = (await hub.create('d0', 'x500_gimbal'))['id']
        record = hub.instances[instance_id]
        fanout = record.telemetry
        now = asyncio.get_running_loop().time()
        fanout.connected = True
        fanout.latest['health'] = {'is_armable': True}
        fanout.received_by_type.update(position=now - 60, health=now)
        result = hub.serialize(record)
        assert result['px4']['connected'] is True
        assert result['px4']['telemetry_fresh'] is False
        assert result['px4']['telemetry_fresh_by_type']['health'] is True
        assert result['px4']['ready_to_arm'] is True
        fanout.received_by_type['health'] = now - 60
        assert hub.serialize(record)['px4']['ready_to_arm'] is False
        record.last_error = 'PX4 process exited'
        assert hub.serialize(record)['binding']['valid'] is True
        await hub.shutdown()
    asyncio.run(run())


def test_binding_unknown_missing_changed_and_recovered(tmp_path):
    async def run():
        hub = service(tmp_path)
        instance_id = (await hub.create('d0', 'x500_gimbal'))['id']
        original = hub.gazebo.drone
        async def missing(_):
            raise HubError(404, 'drone_not_found', 'missing')
        hub.gazebo.drone = missing
        await hub._refresh_gazebo()
        assert (await hub.get(instance_id))['binding']['valid'] is False
        async def unavailable():
            raise HubError(503, 'gazebo_unavailable', 'offline')
        simulation = hub.gazebo.simulation
        hub.gazebo.simulation = unavailable
        await hub._refresh_gazebo()
        result = await hub.get(instance_id)
        assert result['binding']['valid'] is None
        assert result['dependencies']['gazebo_available'] is False
        hub.gazebo.simulation = simulation
        async def changed(drone_id):
            result = await original(drone_id)
            result['entity_id'] += 1
            return result
        hub.gazebo.drone = changed
        await hub._refresh_gazebo()
        assert (await hub.get(instance_id))['binding']['valid'] is False
        hub.gazebo.drone = original
        await hub._refresh_gazebo()
        assert (await hub.get(instance_id))['binding']['valid'] is True
        assert (await hub.get(instance_id))['binding']['error'] is None
        await hub.shutdown()
    asyncio.run(run())


def test_fanout_connection_errors_and_close_wake_clients():
    async def run():
        async def states():
            yield SimpleNamespace(is_connected=True)
            await asyncio.Event().wait()
        fanout = TelemetryFanout(SimpleNamespace(core=SimpleNamespace(connection_state=states)), 'id')
        queue = fanout.subscribe()
        connection = asyncio.create_task(fanout._connection())
        fanout.tasks.append(connection)
        await fanout.connection_received.wait()
        assert fanout.connected is True
        pending = asyncio.create_task(queue.get())
        await fanout.close()
        assert await pending is None
        assert fanout.connected is False and not fanout.tasks
        fanout.unsubscribe(queue)
        assert not fanout.clients
    asyncio.run(run())
