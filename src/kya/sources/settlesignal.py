"""SettleSignal - an independent settlement catalog, used two ways.

`settlesignal.com` publishes a catalog of US settlements as a free CC-BY 4.0
download (`/data/settlements.csv`), regenerated continuously, with per-record
evidence status. It is the pipeline's first *independent primary-grade* source:
until now, every fact on the site came from one index (openclassactions) and
its own case pages, so a mistake in that index had nothing to catch it.

Two channels, both additive and never load-bearing (same contract as
:mod:`kya.xref`):

* **Cross-check by official domain.** A probe against the live data matched
  139 of 277 of our rows by official settlement domain - vs only 12 by title
  similarity, because the two catalogs word titles completely differently.
  Where a matched pair's deadline disagrees, a warning is recorded and *ours
  is kept* (a conflict is information, not an error to auto-resolve); where we
  lack a fact the record's accepted evidence backs, the gap is filled.
* **Import of uncovered cases.** Rows we have no match for, that are open for
  claims (or pay automatically) and carry accepted official evidence, become
  new Settlements - thin ones (no tiers parsed means an Unrated tier), but
  real: deadline, proof, official links, states, status.

Every import or fill is a claim about money and dates, so it is gated on the
record's ``accepted_official_evidence`` flag: a row whose evidence is still
under review is never promoted to a fact.

The catalog is credited in the site colophon and the README; the license
(Credit "SettleSignal (settlesignal.com)" with a link) requires it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Sequence
from urllib.parse import urlparse

from kya.http import FetchError, PoliteClient
from kya.models import Deadline, DeadlineKind, GeoScope, Lane, Settlement
from kya.normalize import normalize_payout
from kya.score import apply_scoring
from kya.xref import similarity

SETTLESIGNAL_CSV_URL = "https://settlesignal.com/data/settlements.csv"

# A domain match alone is not a case match: several administrators file claims
# for many cases through one shared portal host (e.g. forms.ksacms.com serves
# both Kia window-regulator and Banner Health claim forms), so the domain only
# nominates a pair - the titles must also share at least one match token. Live
# calibration: 120 true pairs score 0.11+; the one false pair scores 0.0.
MATCH_TITLE_FLOOR = 0.1


# Only these statuses are worth importing: the reader acts on an open window
# or a payment that happens without them. Closed/paid/pending rows are history,
# and "Published record" is an unclassified catch-all.
_IMPORTABLE_STATUSES = {
    "Open for claims": Lane.CLAIMABLE,
    "Automatic payment": Lane.AUTOMATIC,
}
_LANE_REASON = {
    Lane.CLAIMABLE.value: "published open for claims on settlesignal",
    Lane.AUTOMATIC.value: "automatic payment - published on settlesignal",
}
# Their proof field: yes / no / optional / unknown. `optional` means the basic
# path needs no proof, but there is a documented path too - that is L4's shape
# only when both tiers are actually parsed, so it stays unmapped.
_PROOF_LEVELS = {"no": "L1", "yes": "L3", "optional": None, "unknown": None}


@dataclass
class SettleSignalRecord:
    """One row of the published catalog."""

    title: str
    url: str
    status: str = ""
    category: str = ""
    settlement_type: str = ""
    claim_deadline: str = ""  # ISO date, or ""
    proof_required: str = ""  # yes / no / optional / unknown
    states: list[str] = field(default_factory=list)
    official_claim_url: str = ""
    official_settlement_url: str = ""
    estimated_payout: str = ""
    verification_status: str = ""
    accepted_official_evidence: bool = False
    last_verified: str = ""

    @property
    def deadline_date(self) -> date | None:
        if not self.claim_deadline:
            return None
        try:
            return date.fromisoformat(self.claim_deadline)
        except ValueError:
            return None

    @property
    def evidence_ok(self) -> bool:
        return self.accepted_official_evidence


def parse_settlesignal_csv(text: str) -> list[SettleSignalRecord]:
    """Parse the published CSV. Malformed rows are skipped, not fatal.

    The download is regenerated continuously; a schema tweak must cost a
    smaller catalog, never a collapsed build.
    """
    out: list[SettleSignalRecord] = []
    reader = csv.DictReader(text.lstrip("﻿").splitlines())
    for row in reader:
        if not (row.get("title") and row.get("url")):
            continue
        states = [s.strip().upper() for s in (row.get("applicable_states") or "").split(",") if s.strip()]
        evidence = (row.get("accepted_official_evidence") or "").strip().lower() == "true"
        out.append(
            SettleSignalRecord(
                title=row["title"].strip(),
                url=row["url"].strip(),
                status=(row.get("status") or "").strip(),
                category=(row.get("category") or "").strip(),
                settlement_type=(row.get("settlement_type") or "").strip(),
                claim_deadline=(row.get("claim_deadline") or "").strip(),
                proof_required=(row.get("proof_required") or "").strip().lower(),
                states=states,
                official_claim_url=(row.get("official_claim_url") or "").strip(),
                official_settlement_url=(row.get("official_settlement_url") or "").strip(),
                estimated_payout=(row.get("estimated_payout") or "").strip(),
                verification_status=(row.get("verification_status") or "").strip(),
                accepted_official_evidence=evidence,
                last_verified=(row.get("last_verified") or "").strip(),
            )
        )
    return out


def fetch_records(client: PoliteClient, *, out: Callable[[str], None] = print) -> list[SettleSignalRecord]:
    """Download and parse the catalog; any failure degrades to an empty list."""
    try:
        result = client.get(SETTLESIGNAL_CSV_URL)
    except (FetchError, OSError) as exc:
        out(f"settlesignal: {exc} - continuing without it")
        return []
    if not result.ok:
        out(f"settlesignal: HTTP {result.status_code} - continuing without it")
        return []
    records = parse_settlesignal_csv(result.text)
    out(f"settlesignal: {len(records)} records")
    return records


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host or None


def _index_by_domain(records: Sequence[SettleSignalRecord]) -> dict[str, SettleSignalRecord]:
    """The catalog keyed by every official domain it publishes.

    Claim-portal domains win over settlement-site domains: on the rare row
    where the two differ, the claim portal is the more specific destination.
    """
    index: dict[str, SettleSignalRecord] = {}
    for record in records:
        for url in (record.official_settlement_url, record.official_claim_url):
            domain = _domain(url)
            if domain and domain not in index:
                index[domain] = record
    return index


def _our_domain(settlement: Settlement) -> str | None:
    """The official domain a settlement already carries, if any.

    The settlement site is checked before the claim portal: official sites are
    case-specific, while claim portals are sometimes shared filing platforms.
    """
    return _domain(settlement.official_website) or _domain(settlement.claim_url)


def _cross_check_one(settlement: Settlement, record: SettleSignalRecord) -> None:
    """Compare one matched pair: record conflicts, fill evidence-backed gaps.

    Ours wins every conflict - the warning exists so a human sees it, the way
    index-vs-page conflicts work in :func:`kya.build.merge_page`. Fills only
    happen when we have nothing AND the row carries accepted official evidence.
    """
    ss_deadline = record.deadline_date
    if settlement.deadline.date is not None and ss_deadline is not None:
        if ss_deadline != settlement.deadline.date:
            settlement.warnings.append(
                f"settlesignal lists claim deadline {ss_deadline.isoformat()} but our "
                f"sources say {settlement.deadline.date.isoformat()}; ours kept"
            )
    elif settlement.deadline.date is None and ss_deadline is not None and record.evidence_ok:
        settlement.deadline = Deadline(
            kind=DeadlineKind.CLAIM, raw=record.claim_deadline, date=ss_deadline
        )

    if settlement.claim_url is None and record.official_claim_url and record.evidence_ok:
        settlement.claim_url = record.official_claim_url

    # A proof disagreement only means something when both sides committed:
    # their "unknown" and our None are absences, not opinions.
    ours = settlement.proof_required
    theirs = {"yes": True, "no": False}.get(record.proof_required)
    if ours is not None and theirs is not None and ours != theirs:
        theirs_label = "proof required" if theirs else "no proof needed"
        ours_label = "proof required" if ours else "no proof needed"
        settlement.warnings.append(
            f"settlesignal says {theirs_label}; our sources say {ours_label}"
        )


def _match_ref(record: SettleSignalRecord) -> dict[str, str]:
    return {
        "label": "cross-checked",
        "href": record.url,
        "source": "settlesignal",
        "title": f"SettleSignal: {record.title}",
    }


def _import_settlement(record: SettleSignalRecord) -> Settlement:
    """A thin Settlement for a case no other source covers.

    Thin is honest: whatever the CSV states (deadline, proof, links, states,
    status) is carried verbatim; the payout prose is run through the normal
    parser, and a parse miss means an Unrated tier rather than invented money.
    """
    slug = record.url.rstrip("/").rsplit("/", 1)[-1]
    lane = _IMPORTABLE_STATUSES[record.status]
    tiers = normalize_payout(record.estimated_payout) if record.estimated_payout else []
    settlement = Settlement(
        id=slug,
        source="settlesignal",
        source_url=record.url,
        title=record.title,
        lane=lane,
        lane_reason=_LANE_REASON[lane],
        tiers=tiers,
        proof_required={"yes": True, "no": False}.get(record.proof_required),
        proof_label_raw=record.proof_required.title() if record.proof_required else None,
        proof_level=_PROOF_LEVELS.get(record.proof_required),
        deadline=Deadline(
            kind=DeadlineKind.CLAIM, raw=record.claim_deadline, date=record.deadline_date
        )
        if record.deadline_date
        else Deadline(),
        geo=GeoScope(
            scope_type="states" if record.states else "nationwide",
            states=record.states,
        ),
        status_text=record.status or None,
        official_website=record.official_settlement_url or None,
        claim_url=record.official_claim_url or None,
        verified_as_of=record.last_verified or None,
    )
    return apply_scoring(settlement)


def attach(
    settlements: Sequence[Settlement],
    records: Sequence[SettleSignalRecord],
    *,
    import_new: bool = True,
    out: Callable[[str], None] = print,
) -> list[Settlement]:
    """Cross-check matched pairs, optionally import uncovered cases.

    Both phases are additive; a failure upstream must already have reduced
    ``records`` to empty, in which case this is a no-op.
    """
    if not records:
        return list(settlements)

    by_domain = _index_by_domain(records)
    checked, deadline_conflicts = 0, 0
    matched_domains: set[str] = set()
    out_settlements: list[Settlement] = []
    seen_ids = {s.id for s in settlements}

    for settlement in settlements:
        domain = _our_domain(settlement)
        record = by_domain.get(domain) if domain else None
        if record is not None and similarity(settlement.title, record.title) >= MATCH_TITLE_FLOOR:
            _cross_check_one(settlement, record)
            checked += 1
            if any(w.startswith("settlesignal lists claim deadline") for w in settlement.warnings):
                deadline_conflicts += 1
            if "settlesignal" not in settlement.cross_refs:
                settlement.cross_refs = {**settlement.cross_refs, "settlesignal": [_match_ref(record)]}
            matched_domains.add(domain)
        out_settlements.append(settlement)

    imported = 0
    if import_new:
        for record in records:
            if record.status not in _IMPORTABLE_STATUSES or not record.evidence_ok:
                continue
            domains = {_domain(record.official_claim_url), _domain(record.official_settlement_url)}
            if domains & matched_domains:
                continue  # already covered - this is a match, not a new case
            settlement = _import_settlement(record)
            if settlement.id in seen_ids:
                continue
            seen_ids.add(settlement.id)
            out_settlements.append(settlement)
            imported += 1

    out(
        f"settlesignal: {checked} case(s) cross-checked"
        + (f", {deadline_conflicts} deadline conflict(s) recorded" if deadline_conflicts else "")
        + (f", {imported} new case(s) imported" if imported else "")
    )
    return out_settlements


def enrich(
    settlements: Sequence[Settlement],
    client: PoliteClient,
    *,
    import_new: bool = True,
    out: Callable[[str], None] = print,
) -> list[Settlement]:
    """Fetch the catalog and apply both phases; never raises to the caller."""
    records = fetch_records(client, out=out)
    try:
        return attach(settlements, records, import_new=import_new, out=out)
    except Exception as exc:  # noqa: BLE001 - a matching bug must not sink a build
        out(f"settlesignal matching failed: {exc}")
        return list(settlements)
