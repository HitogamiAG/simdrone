import asyncio
from types import SimpleNamespace

import pytest

from app.errors import BackendError
from app.flight import FlightPlatform
from app.schemas import MissionRun


def test_mission_run_requires_a_revision():
    with pytest.raises(Exception):
        MissionRun.model_validate({"mission_id": "m", "request_id": "r"})


def test_mission_retry_replays_accepted_snapshot_after_edit():
    async def run():
        class Store:
            def __init__(self):
                self.document = {"id": "m", "revision": 1, "waypoints": [{"x": 1}]}
                self.reads = 0

            def get(self, _):
                self.reads += 1
                return self.document

        class Hub:
            async def flight_state(self, _):
                return {"generation": "hub-generation-1"}

            async def flight_start_mission(self, _instance, mission, _request_id, generation):
                assert generation == "hub-generation-1"
                assert mission["revision"] == 1
                return {"execution_id": "execution-1"}

        drone = SimpleNamespace(instance_id="instance-1", generation=4)
        platform = SimpleNamespace(lock=asyncio.Lock(), hub=Hub(), _record=lambda _: drone,
                                   _running=lambda: asyncio.sleep(0))
        flight = FlightPlatform.__new__(FlightPlatform)
        flight.platform = platform
        flight.store = Store()
        flight.executions = {}
        flight.request_ids = {}
        flight.control_requests = {}
        body = {"mission_id": "m", "revision": 1, "request_id": "request-1"}
        accepted = await flight.start_mission("drone-1", body)
        flight.store.document = {"id": "m", "revision": 2, "waypoints": [{"x": 2}]}
        replay = await flight.start_mission("drone-1", body)
        assert replay == accepted
        assert flight.store.reads == 1

    asyncio.run(run())


def test_request_id_conflict_is_checked_before_current_revision():
    async def run():
        class Hub:
            async def flight_state(self, _): return {"generation": "g"}
            async def flight_start_mission(self, *_): return {"execution_id": "e"}

        drone = SimpleNamespace(instance_id="i", generation=1)
        platform = SimpleNamespace(lock=asyncio.Lock(), hub=Hub(), _record=lambda _: drone,
                                   _running=lambda: asyncio.sleep(0))
        flight = FlightPlatform.__new__(FlightPlatform)
        flight.platform, flight.store = platform, SimpleNamespace(get=lambda _: None)
        flight.executions, flight.request_ids, flight.control_requests = {}, {}, {}
        flight.request_ids[("d", "r")] = {"fingerprint": ("m", 1), "response": {"execution_id": "e"}}
        with pytest.raises(BackendError) as error:
            await flight.start_mission("d", {"mission_id": "m", "revision": 2, "request_id": "r"})
        assert error.value.code == "request_id_conflict"

    asyncio.run(run())
