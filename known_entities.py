"""
known_entities.py
Stores and retrieves known entities — vehicles, license plates, animals —
registered by the user through the chat interface.

Faces are handled separately by the existing dlib FaceRecognizer (known_faces/).
This module covers everything else: cars, pets, plates.

Schema:
  known_entities(id, type, name, description, photo_path, plate_text, created_at)

  type: "vehicle" | "license_plate" | "animal"
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("entities")


class KnownEntities:
    def __init__(self, config: dict):
        storage_cfg = config.get("storage", {})
        db_path = storage_cfg.get("db_path", "events.db")
        self.db_path = Path(db_path)

        # Photos stored alongside known_faces/
        self.photos_dir = Path("known_entities")
        self.photos_dir.mkdir(exist_ok=True)

        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS known_entities (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL,    -- vehicle | license_plate | animal
                name        TEXT NOT NULL,    -- e.g. "My Tesla", "Max the dog", "ABC 1234"
                description TEXT,            -- colour, make, model, breed etc.
                photo_path  TEXT,            -- path to reference photo on disk
                plate_text  TEXT,            -- licence plate string if type=license_plate
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entities_type ON known_entities(type);
            """)
        log.info("KnownEntities ready")

    # ── Write ──────────────────────────────────────────────────────────────────

    def register(
        self,
        entity_type: str,
        name: str,
        description: str = "",
        photo_path: Optional[str] = None,
        plate_text: Optional[str] = None,
    ) -> int:
        """Insert a new known entity. Returns the new row ID."""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO known_entities
                   (type, name, description, photo_path, plate_text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (entity_type, name, description, photo_path,
                 plate_text.upper().strip() if plate_text else None,
                 datetime.now().isoformat()),
            )
            eid = cur.lastrowid
        log.info("Registered %s: %s (id=%d)", entity_type, name, eid)
        return eid

    def remove(self, entity_id: int) -> bool:
        """Delete an entity by ID. Returns True if something was deleted."""
        with self._conn() as conn:
            n = conn.execute(
                "DELETE FROM known_entities WHERE id = ?", (entity_id,)
            ).rowcount
        if n:
            log.info("Removed known entity id=%d", entity_id)
        return bool(n)

    def save_photo(self, name: str, entity_type: str, image_bytes: bytes) -> str:
        """Save photo bytes to known_entities/ and return the path."""
        safe = name.replace(" ", "_").replace("/", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{entity_type}_{safe}_{ts}.jpg"
        path = self.photos_dir / filename
        path.write_bytes(image_bytes)
        return str(path)

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_all(self, entity_type: Optional[str] = None) -> list[dict]:
        """Return all known entities, optionally filtered by type."""
        with self._conn() as conn:
            if entity_type:
                rows = conn.execute(
                    "SELECT * FROM known_entities WHERE type = ? ORDER BY name",
                    (entity_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM known_entities ORDER BY type, name"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_by_id(self, entity_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM known_entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_context_string(self) -> str:
        """
        Returns a formatted string injected into the AI Vision prompt
        so Claude knows which vehicles/animals/plates to look for.
        """
        all_entities = self.get_all()
        if not all_entities:
            return ""

        lines = ["Known entities to watch for:"]
        for e in all_entities:
            if e["type"] == "license_plate":
                lines.append(
                    f"  • Licence plate '{e['plate_text']}' belongs to: {e['name']}"
                    + (f" ({e['description']})" if e["description"] else "")
                )
            elif e["type"] == "vehicle":
                lines.append(
                    f"  • Vehicle '{e['name']}': {e['description'] or 'no description'}"
                )
            elif e["type"] == "animal":
                lines.append(
                    f"  • Animal '{e['name']}': {e['description'] or 'no description'}"
                )
        return "\n".join(lines)

    def summary(self) -> dict:
        """Summary counts for the chat status tool."""
        all_e = self.get_all()
        return {
            "faces": len(list(Path("known_faces").glob("*.jpg")))
                     if Path("known_faces").exists() else 0,
            "vehicles": sum(1 for e in all_e if e["type"] == "vehicle"),
            "license_plates": sum(1 for e in all_e if e["type"] == "license_plate"),
            "animals": sum(1 for e in all_e if e["type"] == "animal"),
            "entities": [
                {"id": e["id"], "type": e["type"], "name": e["name"],
                 "description": e["description"], "plate_text": e.get("plate_text")}
                for e in all_e
            ],
        }
