"""The published dataset's contract.

``kya.store`` had no tests at all, yet it is the one module the static site,
the public download link and (later) the notification worker all read. These
pin the two properties that decide whether the dataset can be trusted and
reviewed: it is **deterministic** (a no-op rebuild must not rewrite the
records at all) and it is written **one field per line**, so a reader can see
in a diff exactly which settlement changed.

The determinism test is deliberately specific about *how much* is allowed to
move between builds - at most the timestamp line - because "it looks
deterministic" is how a churning dataset goes unnoticed for months.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kya.store import connect, export_json, load_dataset_file, load_snapshot, save_settlements


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _without_timestamp(path: Path) -> str:
    payload = _read(path)
    payload.pop("generated_at", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _pin_clock(monkeypatch) -> None:
    """Give every timestamped write a distinct, increasing stamp.

    Without this both exports land inside the same second and the tests see
    nothing: any build-time value leaking into the records looks identical
    in both files, which is exactly the bug these tests exist to catch.
    """
    base = datetime(2026, 9, 15, tzinfo=timezone.utc)
    ticks = itertools.count()
    monkeypatch.setattr(
        "kya.store._now",
        lambda: (base + timedelta(seconds=next(ticks))).isoformat(timespec="seconds"),
    )


# --------------------------------------------------------------------------
# export: determinism and reviewability
# --------------------------------------------------------------------------
def test_same_inputs_produce_an_identical_dataset(settlements, tmp_path, monkeypatch) -> None:
    """Two builds of the same sources are the same bytes bar the stamp."""
    _pin_clock(monkeypatch)
    first = export_json(settlements, tmp_path / "a.json", index_updated="2026-09-15")
    second = export_json(settlements, tmp_path / "b.json", index_updated="2026-09-15")
    assert _without_timestamp(first) == _without_timestamp(second)


def test_a_no_op_rebuild_moves_at_most_one_line(settlements, tmp_path, monkeypatch) -> None:
    """This is why a scheduled rebuild is reviewable instead of noise.

    If this ever fails while the underlying data has not changed, the
    dataset has picked up a build-time value somewhere and every run will
    commit a full rewrite - which is precisely how a real change gets lost.
    """
    _pin_clock(monkeypatch)
    first = export_json(settlements, tmp_path / "a.json", index_updated="2026-09-15")
    second = export_json(settlements, tmp_path / "b.json", index_updated="2026-09-15")
    before = first.read_text(encoding="utf-8").splitlines()
    after = second.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after), "a rebuild changed the dataset's line count"
    changed = [i for i, (x, y) in enumerate(zip(before, after)) if x != y]
    assert len(changed) <= 1, f"a no-op rebuild rewrote {len(changed)} lines"


def test_records_are_laid_out_for_human_review(settlements, tmp_path) -> None:
    """One field per line: a compacted dataset cannot be reviewed in a diff."""
    path = export_json(settlements, tmp_path / "d.json")
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n"), "dataset should end with a newline"
    lines = text.splitlines()
    assert lines[0] == "{" and lines[-1] == "}"
    assert len(lines) > len(settlements), "dataset looks compacted onto few lines"


def test_format_is_pinned(settlements, tmp_path) -> None:
    """Exact serialisation: indented, and non-ASCII written as characters."""
    path = export_json(settlements, tmp_path / "d.json")
    text = path.read_text(encoding="utf-8")
    payload = _read(path)
    assert text == json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    literal = [c for c in json.dumps(payload, ensure_ascii=False) if ord(c) > 127]
    if literal:
        assert literal[0] in text, "non-ASCII was escaped instead of written"


def test_payload_shape_and_ordering(settlements, tmp_path) -> None:
    path = export_json(settlements, tmp_path / "d.json", index_updated="2026-09-15")
    payload = _read(path)
    assert set(payload) == {"generated_at", "index_updated", "count", "settlements"}
    assert payload["count"] == len(settlements) == len(payload["settlements"])
    assert payload["index_updated"] == "2026-09-15"
    keys = [(s["lane"], s["kind"], s["title"].lower()) for s in payload["settlements"]]
    assert keys == sorted(keys), "dataset order is not the documented one"


def test_generated_at_is_utc_and_iso(settlements, tmp_path) -> None:
    payload = _read(export_json(settlements, tmp_path / "d.json"))
    stamp = datetime.fromisoformat(payload["generated_at"])
    assert stamp.utcoffset() == timedelta(0), "timestamp should be UTC"


def test_export_creates_missing_directories(settlements, tmp_path) -> None:
    target = export_json(settlements, tmp_path / "data" / "nested" / "out.json")
    assert target.is_file()


def test_every_record_carries_its_provenance(settlements, tmp_path) -> None:
    """Each record must say what it came from, and when it was verified."""
    payload = _read(export_json(settlements, tmp_path / "d.json"))
    for record in payload["settlements"]:
        assert record["source_url"].startswith("http")
        assert record["id"] and record["title"]
        assert record["verified_as_of"] or record["index_updated"]


# --------------------------------------------------------------------------
# sqlite: the memory the diff engine reads
# --------------------------------------------------------------------------
def test_snapshot_round_trips_for_diffing(settlements) -> None:
    conn = connect(":memory:")
    written = save_settlements(conn, settlements)
    assert written == len(settlements)
    snapshot = load_snapshot(conn)
    assert set(snapshot) == {s.id for s in settlements}
    assert snapshot[settlements[0].id] == settlements[0].model_dump(mode="json")


def test_saving_twice_upserts_and_does_not_duplicate(settlements) -> None:
    conn = connect(":memory:")
    save_settlements(conn, settlements)
    save_settlements(conn, settlements)
    rows = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    assert rows == len(settlements)


def test_a_changed_settlement_replaces_the_stored_one(settlements) -> None:
    conn = connect(":memory:")
    original = settlements[0]
    save_settlements(conn, [original])
    edited = original.model_copy(update={"title": original.title + " (amended)"})
    save_settlements(conn, [edited])
    snapshot = load_snapshot(conn)
    assert len(snapshot) == 1
    assert snapshot[original.id]["title"] == edited.title


def test_connect_creates_the_parent_directory(tmp_path) -> None:
    conn = connect(tmp_path / "nested" / "state" / "kya.sqlite3")
    assert (tmp_path / "nested" / "state").is_dir()
    conn.close()


# --- reading the previous build back ------------------------------------------------


def test_load_dataset_file_indexes_the_previous_build_by_id(tmp_path, settlements) -> None:
    """The list-shaped export comes back as ``{id: record}``.

    The published dataset is a *list*, unlike the snapshot's dict, and it is
    what a fresh CI checkout has to hand. Cross-reference carry-over reads it,
    so the two shapes must not be confused.
    """
    path = export_json(settlements, tmp_path / "settlements.json")
    previous = load_dataset_file(path)
    assert set(previous) == {s.id for s in settlements}
    assert previous[settlements[0].id]["case_title"] == settlements[0].case_title


def test_load_dataset_file_treats_missing_and_broken_files_as_no_build(tmp_path) -> None:
    """A first run, and a corrupt file, are both normal states - not failures.

    Raising here would turn a readable-again-tomorrow file into a failed daily
    run, whose cost (no build at all) is far higher than a missing link.
    """
    assert load_dataset_file(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert load_dataset_file(broken) == {}
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"settlements": {"a": {}}}), encoding="utf-8")
    assert load_dataset_file(wrong_shape) == {}


def test_load_dataset_file_skips_records_without_an_id(tmp_path) -> None:
    path = tmp_path / "settlements.json"
    path.write_text(
        json.dumps({"settlements": [{"id": "good"}, {"no_id": True}, "not a record"]}),
        encoding="utf-8",
    )
    assert load_dataset_file(path) == {"good": {"id": "good"}}