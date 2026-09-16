"""Change detection: what happened since the last poll?

The diff engine compares the *stored* snapshot (SQLite, via
:mod:`kya.store`) against the freshly assembled records and emits typed
events. It answers four questions and no more:

* is this case **new** to me?
* did the **money** change?
* did the **deadline** move - and in the claimant's favour or against it?
* is a deadline **about to close** (or just closed)?

Lane changes are watched too, because a case sliding from "claimable" to
"pending" is exactly the news a claimant acts on. Everything else is
deliberately ignored: description rewording is not a notification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Mapping, Sequence


class EventKind(str, Enum):
    NEW = "new"
    REMOVED = "removed"
    PAYOUT_CHANGED = "payout_changed"
    DEADLINE_EXTENDED = "deadline_extended"
    DEADLINE_MOVED_UP = "deadline_moved_up"
    DEADLINE_SOON = "deadline_soon"
    DEADLINE_PASSED = "deadline_passed"
    LANE_CHANGED = "lane_changed"


@dataclass(frozen=True)
class DiffEvent:
    """One thing that changed for one case."""

    kind: EventKind
    settlement_id: str
    title: str
    detail: str = ""

    @property
    def is_actionable(self) -> bool:
        """Removals are bookkeeping, not news worth waking anyone for."""
        return self.kind is not EventKind.REMOVED


def _payout_figure(record: Mapping) -> float | None:
    """The single number that best represents 'what can I get'.

    The ceiling of the most generous tier. Tiers change shape as sources
    reword them; a figure-to-figure comparison survives rewording.
    """
    ceiling = None
    for tier in record.get("tiers") or []:
        amount = tier.get("amount_max") or tier.get("per_unit") or tier.get("amount_min")
        if amount is not None and (ceiling is None or amount > ceiling):
            ceiling = amount
    return ceiling


def _deadline_date(record: Mapping) -> str | None:
    return (record.get("deadline") or {}).get("date")


def _parse_iso(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _figure_change(old: float | None, new: float | None) -> str:
    def fmt(value: float | None) -> str:
        return "none" if value is None else f"${value:,.2f}".rstrip("0").rstrip(".")

    return f"{fmt(old)} -> {fmt(new)}"


def _deadline_events(old, record, today, soon_cutoff) -> list[DiffEvent]:
    """Deadline movement, soon-window entry, and closing."""
    events: list[DiffEvent] = []
    old_text = _deadline_date(old)
    new_text = _deadline_date(record)
    old_day, new_day = _parse_iso(old_text), _parse_iso(new_text)

    if old_day is not None and new_day is not None and new_day != old_day:
        kind = EventKind.DEADLINE_EXTENDED if new_day > old_day else EventKind.DEADLINE_MOVED_UP
        events.append(DiffEvent(kind, record["id"], record["title"], f"{old_text} -> {new_text}"))

    # A deadline that just entered the soon window: already-soon deadlines in
    # the previous snapshot must not re-fire on every poll.
    if (
        new_day is not None
        and today <= new_day <= soon_cutoff
        and not (old_day is not None and today <= old_day <= soon_cutoff)
    ):
        events.append(
            DiffEvent(EventKind.DEADLINE_SOON, record["id"], record["title"], f"closes {new_text}")
        )
    elif new_day is not None and new_day < today and (old_day is None or old_day >= today):
        events.append(
            DiffEvent(
                EventKind.DEADLINE_PASSED, record["id"], record["title"], f"closed {new_text}"
            )
        )
    return events


def diff_snapshots(
    previous: Mapping[str, Mapping],
    current: Sequence[Mapping],
    *,
    today: date | None = None,
    soon_days: int = 7,
) -> list[DiffEvent]:
    """Compare a stored snapshot against freshly built records.

    ``previous`` is ``{id: record}`` from :func:`kya.store.load_snapshot`;
    ``current`` is a sequence of ``Settlement.model_dump(mode="json")``
    records. Deterministic output, ordered by kind then id.
    """
    today = today or date.today()
    soon_cutoff = today + timedelta(days=soon_days)
    current_by_id = {record["id"]: record for record in current}

    events: list[DiffEvent] = []

    for record in current:
        sid = record["id"]
        old = previous.get(sid)
        if old is None:
            events.append(DiffEvent(EventKind.NEW, sid, record["title"]))
            continue

        if old.get("lane") != record.get("lane"):
            events.append(
                DiffEvent(
                    EventKind.LANE_CHANGED,
                    sid,
                    record["title"],
                    f"{old.get('lane')} -> {record.get('lane')}",
                )
            )

        old_amount = _payout_figure(old)
        new_amount = _payout_figure(record)
        if old_amount != new_amount:
            events.append(
                DiffEvent(
                    EventKind.PAYOUT_CHANGED,
                    sid,
                    record["title"],
                    _figure_change(old_amount, new_amount),
                )
            )

        events.extend(_deadline_events(old, record, today, soon_cutoff))

    for sid, old in previous.items():
        if sid not in current_by_id:
            events.append(DiffEvent(EventKind.REMOVED, sid, old.get("title", sid)))

    order = {kind: i for i, kind in enumerate(EventKind)}
    events.sort(key=lambda e: (order[e.kind], e.settlement_id))
    return events

