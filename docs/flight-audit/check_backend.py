"""Аудит Backend на подменённом Hub. Exit 1 означает нарушение контракта."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import httpx

from app.adapters import HubApi
from app.flight import FlightPlatform
from app.schemas import MissionRun


async def main():
    failed = 0
    with tempfile.TemporaryDirectory() as directory:
        class Hub:
            async def flight_start_mission(self, *_):
                return {"execution_id": "execution"}

        async def running():
            return {}

        platform = SimpleNamespace(settings=SimpleNamespace(mission_db=Path(directory)/"missions.sqlite3"),
            lock=asyncio.Lock(), _record=lambda _: SimpleNamespace(instance_id="i", generation=1),
            _running=running, hub=Hub())
        flight = FlightPlatform(platform)
        mission = {"id": "m", "revision": 1, "world": "empty", "name": "route"}
        flight.store.create(mission)

        async def schema_default_revision_can_start():
            body = MissionRun(mission_id="m", request_id="optional").model_dump()
            result = await flight.start_mission("d", body)
            assert result["execution_id"] == "execution"

        async def idempotent_replay_survives_definition_edit():
            body = MissionRun(mission_id="m", revision=1, request_id="repeat").model_dump()
            accepted = await flight.start_mission("d", body)
            flight.store.update("m", 1, mission)
            repeated = await flight.start_mission("d", body)
            assert repeated == accepted, "Replay returned another operation"

        async def generation_is_forwarded_to_hub():
            captured = []

            def handler(request):
                captured.append(json.loads(request.content))
                return httpx.Response(202, json={"execution_id": "execution"})

            adapter = HubApi("http://hub", 5)
            await adapter.client.aclose()
            adapter.client = httpx.AsyncClient(base_url="http://hub", transport=httpx.MockTransport(handler))
            try:
                await adapter.flight_start_mission("i", mission, "generation", 7)
                assert captured[0].get("generation") == 7, f"Generation not forwarded: {captured[0]}"
            finally:
                await adapter.close()

        for check in (schema_default_revision_can_start, idempotent_replay_survives_definition_edit,
                      generation_is_forwarded_to_hub):
            try:
                await check()
            except Exception as exc:
                failed += 1
                print(json.dumps({"check": check.__name__, "passed": False, "error": str(exc)}), flush=True)
            else:
                print(json.dumps({"check": check.__name__, "passed": True}), flush=True)
        flight.close()
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
