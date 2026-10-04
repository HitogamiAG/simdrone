"""SQLite storage for immutable mission revisions."""
import json
import sqlite3
from pathlib import Path


class MissionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, world TEXT NOT NULL,
                revision INTEGER NOT NULL, document TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def list(self, world=None):
        with self._connect() as db:
            rows = db.execute("SELECT document FROM missions WHERE world=? ORDER BY name, id", (world,)).fetchall() if world else db.execute("SELECT document FROM missions ORDER BY world, name, id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, mission_id):
        with self._connect() as db:
            row = db.execute("SELECT document FROM missions WHERE id=?", (mission_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, document):
        encoded = json.dumps(document, separators=(",", ":"), allow_nan=False)
        with self._connect() as db:
            db.execute("INSERT INTO missions VALUES (?, ?, ?, ?, ?)",
                       (document["id"], document["name"], document["world"], 1, encoded))
        return document

    def update(self, mission_id, expected_revision, document):
        current = self.get(mission_id)
        if current is None:
            return None, "missing"
        if current["revision"] != expected_revision:
            return current, "revision_conflict"
        document = {**document, "id": mission_id, "revision": expected_revision + 1}
        encoded = json.dumps(document, separators=(",", ":"), allow_nan=False)
        with self._connect() as db:
            changed = db.execute("UPDATE missions SET name=?, world=?, revision=?, document=? WHERE id=? AND revision=?",
                                 (document["name"], document["world"], document["revision"], encoded,
                                  mission_id, expected_revision)).rowcount
        return (document, None) if changed else (self.get(mission_id), "revision_conflict")

    def delete(self, mission_id):
        with self._connect() as db:
            return db.execute("DELETE FROM missions WHERE id=?", (mission_id,)).rowcount > 0
