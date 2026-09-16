"""Lane and kind classification - the deterministic priority table.

Lane assignment is a *classification rule*, not a judgement call, and it is
ordered: an entry is checked against each lane's predicate in priority order
and the first match wins. Every decision records a ``lane_reason`` so the site
(and the tests) can show *why* an entry sits in its lane.

    3. investigation - /lawsuits/ URLs and "free case review" invitations
    4. pending       - "pending final approval" / "not yet verified"
    2. automatic     - "Automatic payment - no claim form"
    1. claimable     - everything else with an open claim window
"""

from __future__ import annotations

from kya.models import CaseKind, Lane
from kya.sources.openclassactions_index import IndexEntry
from kya.textutil import collapse_ws

# Lane 3: invitations to join an investigation, where nothing is claimable yet.
# Some of these are not caught by the index parser's label matcher because the
# marker lives in the title instead ("No Settlement or Claim Form").
_INVESTIGATION_TITLE_MARKERS = (
    "no settlement or claim form",
    "free case review",
)

# Lane 4: a settlement exists but the claim window is not confirmed yet.
_PENDING_MARKERS = (
    "pending final approval",
    "not yet verified",
)

# Kind detection, from URL path then title. Ordered: a "recall" inside a
# /lawsuits/ path is still a recall first, because its remedy is the recall
# program rather than the suit ("recall remedy only; nothing to claim").
_RECALL_TITLE_MARKERS = ("recall",)


def classify_kind(entry: IndexEntry) -> CaseKind:
    """What a case *is*, harvested from the URL path and title."""
    path = entry.url.lower()
    title = entry.title.lower()
    if "/lawsuits/" in path:
        if any(marker in title for marker in _RECALL_TITLE_MARKERS):
            return CaseKind.RECALL
        return CaseKind.INVESTIGATION
    if any(marker in title for marker in _RECALL_TITLE_MARKERS):
        return CaseKind.RECALL
    return CaseKind.SETTLEMENT


def classify_lane(entry: IndexEntry) -> tuple[Lane, str]:
    """Assign one of the four lanes; first matching predicate wins."""
    title = collapse_ws(entry.title).lower()
    notes = " ".join(entry.notes).lower()

    # --- Lane 3: investigations to join ---------------------------------
    # Checked first: an investigation can carry neither a deadline nor a
    # payout, so it must never fall through to "claimable".
    if "/lawsuits/" in entry.url.lower():
        return Lane.INVESTIGATION, "lawsuit page under /lawsuits/"
    if entry.is_investigation:
        return Lane.INVESTIGATION, "index labels this an investigation"
    if any(marker in title or marker in notes for marker in _INVESTIGATION_TITLE_MARKERS):
        return Lane.INVESTIGATION, "no settlement or claim form - invitation to join"

    # --- Lane 4: pending / not yet verified ------------------------------
    if entry.is_pending:
        return Lane.PENDING, "listed under 'Recent Settlements and Lawsuits'"
    if any(marker in title or marker in notes for marker in _PENDING_MARKERS):
        return Lane.PENDING, "pending final approval - claim window not confirmed"

    # --- Lane 2: automatic, no action needed -----------------------------
    if entry.proof_level == "L0":
        return Lane.AUTOMATIC, "automatic payment - no claim form"

    # --- Lane 1: claimable now -------------------------------------------
    return Lane.CLAIMABLE, "open claim window"