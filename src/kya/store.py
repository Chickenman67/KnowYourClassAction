"""Persistence: SQLite for diffing across runs, JSON for publishing.

SQLite is the *memory* - it survives between polls so the diff engine can
answer "what changed?". The JSON file is the *publication* - it is what the
static site and (later) the Cloudflare Worker read, and it carries no state
beyond the current snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from kya.models import Settlement

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settlements (
    id              TEXT PRIMARY KEY,
    lane            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payout_tier     TEXT,
    ev_estimate     REAL,
    ev_confidence   TEXT,
    deadline_kind   TEXT,
    deadline_date   TEXT,
    title           TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    record          TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_lane ON settlements(lane);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the tracker database. ``:memory:`` is used when no path is given."""
    if path is None or str(path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_settlements(conn: sqlite3.Connection, settlements: list[Settlement]) -> int:
    """Upsert a full snapshot. Returns the number of rows written."""
    now = _now()
    rows = []
    for s in settlements:
        record = s.model_dump(mode="json")
        rows.append(
            (
                s.id,
                str(s.lane),
                str(s.kind),
                s.payout_tier,
                s.ev_estimate,
                str(getattr(s.ev_confidence, "value", s.ev_confidence)),
                str(s.deadline.kind),
                s.deadline.date.isoformat() if s.deadline.date else None,
                s.title,
                s.source_url,
                s.content_hash,
                json.dumps(record, ensure_ascii=False),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO settlements
            (id, lane, kind, payout_tier, ev_estimate, ev_confidence,
             deadline_kind, deadline_date, title, source_url,
             content_hash, record, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            lane=excluded.lane, kind=excluded.kind,
            payout_tier=excluded.payout_tier, ev_estimate=excluded.ev_estimate,
            ev_confidence=excluded.ev_confidence,
            deadline_kind=excluded.deadline_kind,
            deadline_date=excluded.deadline_date, title=excluded.title,
            source_url=excluded.source_url, content_hash=excluded.content_hash,
            record=excluded.record, updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_snapshot(conn: sqlite3.Connection) -> dict[str, dict]:
    """The stored snapshot as ``{id: record}``, for the diff engine."""
    return {
        row[0]: json.loads(row[1])
        for row in conn.execute("SELECT id, record FROM settlements")
    }


def export_json(
    settlements: list[Settlement],
    path: Path | str,
    *,
    index_updated: str | None = None,
) -> Path:
    """Write the published dataset. Deterministic output: lane, then title."""
    ordered = sorted(
        settlements,
        key=lambda s: (str(s.lane), str(s.kind), s.title.lower()),
    )
    payload = {
        "generated_at": _now(),
        "index_updated": index_updated,
        "count": len(ordered),
        "settlements": [s.model_dump(mode="json") for s in ordered],
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    return target
