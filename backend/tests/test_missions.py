import tempfile
from pathlib import Path

from app.missions import MissionStore
from app.main import create_app
from app.config import Settings


def test_mission_revisions_are_persisted_and_updates_use_compare_and_swap():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "missions.sqlite3"
        store = MissionStore(path)
        mission = {"id": "m1", "name": "route", "world": "empty", "revision": 1,
                   "waypoints": [{"x": 1.0, "y": 2.0, "z": 4.0}]}
        store.create(mission)

        reopened = MissionStore(path)
        assert reopened.get("m1") == mission
        changed, error = reopened.update("m1", 1, {**mission, "name": "updated"})
        assert error is None
        assert changed["revision"] == 2
        conflict, error = reopened.update("m1", 1, mission)
        assert error == "revision_conflict"
        assert conflict["revision"] == 2
        assert reopened.delete("m1")
        assert reopened.get("m1") is None


def test_flight_and_mission_routes_are_in_openapi():
    app = create_app(settings=Settings(mission_db=":memory:"))
    paths = app.openapi()["paths"]
    assert "/api/v1/missions/" in paths
    assert "/api/v1/missions/{mission_id}/validate" in paths
    assert "/api/v1/drones/{drone_id}/flight/missions" in paths
    assert "/api/v1/drones/{drone_id}/flight/offboard/sessions/{session_id}/{action}" in paths
