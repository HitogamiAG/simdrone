"""Real Compose checks. Run inside Hub; uses no Docker socket or host Gazebo."""
import argparse
import asyncio
import json
import os
import signal
import time
import uuid
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

HUB = 'http://127.0.0.1:8002'
GAZEBO = os.getenv('GAZEBO_API_URL', 'http://gazebo-service:8000')


async def request(client, method, url, expected=200, **kwargs):
    response = await client.request(method, url, **kwargs)
    assert response.status_code == expected, (method, url, response.status_code, response.text)
    return response.json()


async def eventually(client, instance_id, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await request(client, 'GET', f'{HUB}/api/v1/instances/{instance_id}/')
        if predicate(result):
            return result
        await asyncio.sleep(0.25)
    raise AssertionError(f'diagnostic did not converge: {result}')


def cgroup_usage():
    root = Path('/sys/fs/cgroup')
    if (root / 'cpu.stat').exists():
        stats = dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())
        return int(stats['usage_usec']), int((root / 'memory.current').read_text())
    return int((root / 'cpuacct/cpuacct.usage').read_text()) // 1000, int((root / 'memory/memory.usage_in_bytes').read_text())


async def resources(count):
    before, _ = cgroup_usage()
    start = time.monotonic()
    await asyncio.sleep(5)
    after, memory = cgroup_usage()
    return {'instances': count, 'hub_cpu_percent_one_core': round((after-before)/1e6/(time.monotonic()-start)*100, 1),
            'hub_memory_mib': round(memory / 1024**2, 1)}


async def socket_closed(socket):
    try:
        async with asyncio.timeout(10):
            while True:
                await socket.recv()
    except ConnectionClosed as exc:
        assert exc.rcvd is not None and exc.rcvd.code == 1012, exc
    else:
        raise AssertionError('old WebSocket generation remained open')


async def main(mode):
    async with httpx.AsyncClient(timeout=130) as client:
        if mode == 'loss':
            records = await request(client, 'GET', f'{HUB}/api/v1/instances/')
            assert records
            await eventually(client, records[0]['id'], lambda r: not r['dependencies']['gazebo_available'] and r['binding']['valid'] is None)
            print('PASS unexpected Gazebo loss: binding unknown; Hub healthy', flush=True)
            assert await request(client, 'GET', f'{HUB}/healthz') == {'status': 'ok'}
            return
        if mode == 'cleanup':
            for record in await request(client, 'GET', f'{HUB}/api/v1/instances/'):
                await request(client, 'DELETE', f"{HUB}/api/v1/instances/{record['id']}/")
            return
        assert await request(client, 'GET', f'{HUB}/api/v1/instances/') == [], 'stop existing instances before this test'
        assert await request(client, 'GET', f'{GAZEBO}/api/v1/drones/') == [], 'integration requires an empty world'
        prefix = 'hub-check-' + uuid.uuid4().hex[:8]
        drones, instances = [], []
        report = {'start_seconds': [], 'resources': []}
        fixture = mode == 'fixture'
        leave = False
        try:
            for n in range(1 if fixture else 4):
                drone = await request(client, 'POST', f'{GAZEBO}/api/v1/drones/', expected=201,
                                      json={'model': 'x500_gimbal', 'name': f'{prefix}-{n}',
                                            'pose': {'position': {'x': n*3, 'y': 0, 'z': 0.3}, 'orientation': {'x': 0, 'y': 0, 'z': 0, 'w': 1}}})
                drones.append(drone['id'])
            if fixture:
                result = await request(client, 'POST', f'{HUB}/api/v1/instances/', expected=201, json={'drone_id': drones[0]})
                leave = True
                print(json.dumps({'fixture': result['id'], 'process': result['process']}), flush=True)
                return

            async def create(drone_id):
                start = time.monotonic()
                result = await request(client, 'POST', f'{HUB}/api/v1/instances/', expected=201, json={'drone_id': drone_id})
                instances.append(result['id'])
                report['start_seconds'].append(round(time.monotonic()-start, 2))
                assert result['px4']['connected'] and result['px4']['telemetry_fresh'], result
                return result
            pair = await asyncio.gather(create(drones[0]), create(drones[1]))
            report['resources'].append(await resources(2))
            third = await create(drones[2])
            report['resources'].append(await resources(3))
            assert {r['px4']['system_id'] for r in [*pair, third]} == {1, 2, 3}
            duplicate = await request(client, 'POST', f'{HUB}/api/v1/instances/', expected=409, json={'drone_id': drones[0]})
            assert duplicate['error']['code'] == 'drone_already_bound'
            limit = await request(client, 'POST', f'{HUB}/api/v1/instances/', expected=409, json={'drone_id': drones[3]})
            assert limit['error']['code'] == 'instance_limit'
            first, second = pair[0]['id'], pair[1]['id']
            base = f'{HUB}/api/v1/instances/{first}'
            neighbour_before = await request(client, 'GET', f'{HUB}/api/v1/instances/{second}/parameters/')
            await request(client, 'PATCH', base+'/parameters/', json={'MPC_XY_VEL_MAX': 10.5})
            assert neighbour_before == await request(client, 'GET', f'{HUB}/api/v1/instances/{second}/parameters/')
            ws_url = f'ws://127.0.0.1:8002/api/v1/instances/{first}/telemetry'
            async with connect(ws_url) as fast, connect(ws_url, max_queue=1) as slow:
                # The slow client intentionally does not consume while the fast one does.
                async with asyncio.timeout(10):
                    for _ in range(20):
                        sample = json.loads(await fast.recv())
                        assert sample['instance_id'] == first
                await asyncio.sleep(2)
                assert (await request(client, 'GET', base+'/'))['status'] == 'running'
                await request(client, 'POST', base+'/restart')
                await asyncio.gather(socket_closed(fast), socket_closed(slow))
            params = await request(client, 'GET', base+'/parameters/')
            assert abs(params['MPC_XY_VEL_MAX'] - 10.5) < 1e-4
            await request(client, 'POST', f'{GAZEBO}/api/v1/world/pause')
            try:
                for method, url, payload in [('POST', base+'/restart', None), ('PATCH', base+'/parameters/', {'MPC_XY_VEL_MAX': 9}),
                                             ('POST', f'{HUB}/api/v1/instances/', {'drone_id': drones[3]})]:
                    result = await request(client, method, url, expected=409, json=payload)
                    assert result['error']['code'] == 'simulation_paused'
                await asyncio.sleep(6)
                assert (await request(client, 'GET', base+'/'))['process']['alive']
            finally:
                await request(client, 'POST', f'{GAZEBO}/api/v1/world/resume')
            await eventually(client, first, lambda r: r['px4']['telemetry_fresh'])
            for process in ('px4_pid', 'mavsdk_pid'):
                result = await request(client, 'GET', base+'/')
                os.kill(result['process'][process], signal.SIGKILL)
                failed = await eventually(client, first, lambda r: r['status'] == 'failed' and r['process']['px4_pid'] is None and r['process']['mavsdk_pid'] is None)
                assert failed['process']['px4_pid'] is None and failed['process']['mavsdk_pid'] is None
                assert (await request(client, 'GET', f'{HUB}/api/v1/instances/{second}/'))['process']['alive']
                await request(client, 'POST', base+'/restart')
            async with connect(ws_url) as socket:
                await socket.recv()
                await request(client, 'DELETE', base+'/')
                instances.remove(first)
                await socket_closed(socket)
            # Coordinated drone-reset: stop, reset, bind again; API id is preserved.
            await request(client, 'POST', f'{GAZEBO}/api/v1/drones/{drones[0]}/reset')
            reset_instance = await create(drones[0])
            await request(client, 'DELETE', f"{HUB}/api/v1/instances/{reset_instance['id']}/")
            instances.remove(reset_instance['id'])
            # Unexpected model disappearance is diagnostic only.
            await request(client, 'DELETE', f'{GAZEBO}/api/v1/drones/{drones[2]}/')
            drones.remove(drones[2])
            await eventually(client, third['id'], lambda r: r['binding']['valid'] is False)
            for instance_id in instances.copy():
                await request(client, 'DELETE', f'{HUB}/api/v1/instances/{instance_id}/')
                instances.remove(instance_id)
            # Only reset after all Hub instances have stopped.
            await request(client, 'POST', f'{GAZEBO}/api/v1/world/world-reset')
            drones.clear()
            assert await request(client, 'GET', f'{GAZEBO}/api/v1/drones/') == []
            drone = await request(client, 'POST', f'{GAZEBO}/api/v1/drones/', expected=201,
                                  json={'model': 'x500_gimbal', 'name': prefix+'-after-reset'})
            drones.append(drone['id'])
            await create(drone['id'])
            print('PASS real concurrent create, parameters, WS, pause, process failures, coordinated reset', flush=True)
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            if not leave:
                for instance_id in instances:
                    await request(client, 'DELETE', f'{HUB}/api/v1/instances/{instance_id}/')
                for drone_id in drones:
                    response = await client.delete(f'{GAZEBO}/api/v1/drones/{drone_id}/')
                    assert response.status_code in (200, 404), response.text
                assert not list(Path('/opt/uav/instances').iterdir()), 'instance directories leaked'
                leaked = []
                for process in Path('/proc').iterdir():
                    if not process.name.isdigit():
                        continue
                    try:
                        command = (process / 'cmdline').read_bytes().split(b'\0')[0]
                    except (FileNotFoundError, PermissionError, ProcessLookupError):
                        continue
                    if command and Path(os.fsdecode(command)).name in ('px4', 'mavsdk_server'):
                        leaked.append(process.name)
                assert not leaked, f'owned processes leaked: {leaked}'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('full', 'fixture', 'loss', 'cleanup'), default='full')
    asyncio.run(main(parser.parse_args().mode))
