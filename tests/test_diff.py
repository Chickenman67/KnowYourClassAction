"""Change detection - the diff engine's four questions, tested in isolation.

These use hand-built records (the shape ``Settlement.model_dump(mode="json")``
produces) so a failure points at the diff logic itself, not at the parsers.
"""

from __future__ import annotations

from datetime import date, timedelta

from kya.diff import EventKind, diff_snapshots

TODAY = date(2026, 9, 14)


def record(
    sid: str = "alpha",
    lane: str = "claimable",
    title: str = "Alpha Settlement",
    amount_max: float | None = 100.0,
    deadline_date: str | None = "2026-11-01",
) -> dict:
    tiers = [] if amount_max is None else [
        {"amount_max": amount_max, "amount_min": None, "per_unit": None}
    ]
    return {
        "id": sid,
        "title": title,
        "lane": lane,
        "tiers": tiers,
        "deadline": {"date": deadline_date},
    }


def snapshot(*records: dict) -> dict[str, dict]:
    return {r["id"]: r for r in records}


# --- is this case new to me? -------------------------------------------------
def test_first_poll_reports_every_case_as_new() -> None:
    events = diff_snapshots({}, [record("a"), record("b")], today=TODAY)
    assert [e.kind for e in events] == [EventKind.NEW, EventKind.NEW]
    assert all(e.is_actionable for e in events)


def test_removal_is_reported_but_not_actionable() -> None:
    events = diff_snapshots(snapshot(record("a")), [], today=TODAY)
    assert [e.kind for e in events] == [EventKind.REMOVED]
    assert events[0].is_actionable is False


# --- did the money change? ---------------------------------------------------
def test_no_change_produces_no_events() -> None:
    events = diff_snapshots(snapshot(record()), [record()], today=TODAY)
    assert events == []


def test_secondary_source_links_are_not_news() -> None:
    """A changing ``cross_refs`` set must never notify anyone.

    The news feed rotates its 100 items continuously, and a match may appear or
    vanish between polls as a headline rolls off the end - so if the diff
    watched cross-references, a quiet day would buzz the phone about links.
    They are evidence attached to a case, not a change *to* the case.
    """
    old = {**record(), "cross_refs": {}}
    new = {
        **record(),
        "cross_refs": {
            "news": [
                {"label": "in the news", "href": "https://tca/a/", "source": "topclassactions"},
                {"label": "in the news", "href": "https://tca/b/", "source": "topclassactions"},
            ],
            "docket": [
                {"label": "docket", "href": "https://cl/d/1/", "source": "courtlistener"}
            ],
        },
    }
    assert diff_snapshots(snapshot(old), [new], today=TODAY) == []
    assert diff_snapshots(snapshot(new), [old], today=TODAY) == []


def test_payout_increase_is_detected_figure_to_figure() -> None:
    old = record(amount_max=100.0)
    new = record(amount_max=250.0)
    events = diff_snapshots(snapshot(old), [new], today=TODAY)
    payout = [e for e in events if e.kind is EventKind.PAYOUT_CHANGED]
    assert len(payout) == 1
    assert "100" in payout[0].detail and "250" in payout[0].detail


def test_payout_figure_survives_a_reworded_tier_shape() -> None:
    """Same money, different shape (fixed -> per-unit) must NOT look like news."""
    old = record()
    new = record()
    new["tiers"] = [{"amount_max": None, "amount_min": None, "per_unit": 100.0}]
    events = diff_snapshots(snapshot(old), [new], today=TODAY)
    assert [e.kind for e in events if e.kind is EventKind.PAYOUT_CHANGED] == []


# --- did the deadline move? --------------------------------------------------
def test_extension_and_moving_up_are_distinguished() -> None:
    old = snapshot(record(deadline_date="2026-10-01"))
    extended = diff_snapshots(old, [record(deadline_date="2026-12-01")], today=TODAY)
    moved_up = diff_snapshots(old, [record(deadline_date="2026-09-20")], today=TODAY)
    assert [e.kind for e in extended] == [EventKind.DEADLINE_EXTENDED]
    assert [e.kind for e in moved_up if e.kind is EventKind.DEADLINE_MOVED_UP] == [
        EventKind.DEADLINE_MOVED_UP
    ]


def test_soon_window_fires_once_not_every_poll() -> None:
    far = (TODAY + timedelta(days=60)).isoformat()
    soon = (TODAY + timedelta(days=5)).isoformat()
    # Entering the window fires, even alongside the movement event.
    entered = diff_snapshots(snapshot(record(deadline_date=far)), [record(deadline_date=soon)],
                             today=TODAY)
    assert [e.kind for e in entered if e.kind is EventKind.DEADLINE_SOON] == [
        EventKind.DEADLINE_SOON
    ]
    # Already-soon in the previous snapshot: staying soon is not news.
    again = diff_snapshots(
        snapshot(record(deadline_date=soon)),
        [record(deadline_date=soon)],
        today=TODAY + timedelta(days=1),
    )
    assert [e.kind for e in again if e.kind is EventKind.DEADLINE_SOON] == []


def test_passed_deadline_is_reported_when_it_happens() -> None:
    closed = (TODAY - timedelta(days=1)).isoformat()
    events = diff_snapshots(
        snapshot(record(deadline_date=(TODAY + timedelta(days=1)).isoformat())),
        [record(deadline_date=closed)],
        today=TODAY,
    )
    kinds = {e.kind for e in events}
    assert EventKind.DEADLINE_MOVED_UP in kinds
    assert EventKind.DEADLINE_PASSED in kinds


def test_lane_change_is_news_because_it_is_what_claimants_act_on() -> None:
    events = diff_snapshots(snapshot(record(lane="claimable")), [record(lane="pending")],
                            today=TODAY)
    changed = [e for e in events if e.kind is EventKind.LANE_CHANGED]
    assert len(changed) == 1
    assert "claimable -> pending" in changed[0].detail


def test_output_is_deterministic_and_ordered() -> None:
    old = snapshot(record("a"), record("b"))
    new = [record("b", amount_max=999.0), record("a", amount_max=999.0), record("c")]
    events = diff_snapshots(old, new, today=TODAY)
    keys = [(e.kind, e.settlement_id) for e in events]
    assert keys == sorted(keys, key=lambda k: (list(EventKind).index(k[0]), k[1]))
