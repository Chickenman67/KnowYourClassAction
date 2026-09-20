"""Static site builder: settlements -> docs/ (a legal gazette, not a SaaS grid).

Design intent (settled in planning):

* an editorial *ledger* - typographic hierarchy, hairline rules, tabular
  numerals - not a card grid;
* the one memorable element is the **deadline runway**: a horizontal time
  ruler at the top where every open claim is a tick that slides left as its
  window closes, colored by payout tier;
* the four lanes are visually separate sections, because "what closes
  soonest", "what needs nothing from me", "what I can join" and "what isn't
  verified yet" are four different questions;
* unknown data is labeled unknown, never invented: an unset EV prints as an
  em-dash, a missing proof level prints "unknown", and every figure carries
  the "verified as of" stamp of the index it came from.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from kya.config import load_config
from kya.models import Settlement

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_PROOF_BADGES = {
    "L0": "Automatic",
    "L1": "No proof",
    "L2": "Notice ID",
    "L3": "Documents",
    "L4": "Dual-tier",
}

# Secondary-source link labels, keyed by the ref kind kya.xref attaches. A
# docket link is a court record (independent verification), a news link is
# another outlet's coverage - so they must not read the same. A SettleSignal or
# ClaimDepot link means the case's facts were checked against a second full
# catalog; both carry the same label because they make the same promise.
_XREF_LABELS = {
    "news": "in the news",
    "docket": "docket",
    "settlesignal": "cross-checked",
    "claimdepot": "cross-checked",
}

_DEADLINE_PREFIX = {
    "claim": "Claim by",
    "opt_out": "Opt out by",
    "objection": "Object by",
    "optional_election": "Election by",
    "rolling": "Rolling",
    "no_action": "No action needed",
    "not_yet_known": "Not yet scheduled",
    "conditional": "Conditional",
    "unparsed": "Deadline",
}

_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _fmt_date(value: date_cls | None) -> str:
    if value is None:
        return ""
    return f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"


def _fmt_money(value: float) -> str:
    if value >= 100:
        return f"${value:,.0f}"
    return f"${value:,.2f}".rstrip("0").rstrip(".")


def money_sticker(settlement: Settlement) -> str:
    """A short human phrase for what you can get - honest about its type."""
    tier = settlement.best_tier()
    if tier is None:
        return settlement.payout_raw or "—"
    kind = str(tier.payout_type)
    ceiling = tier.ceiling()
    if kind == "pro_rata_fund":
        if settlement.fund_size:
            return f"Pro rata of {_fmt_money(settlement.fund_size)} fund"
        return "Pro rata share"
    if kind == "voucher_or_credit":
        return f"{_fmt_money(ceiling)} voucher" if ceiling else "Voucher"
    if kind == "non_cash":
        return "Non-cash remedy"
    if kind == "undisclosed":
        return "Varies"
    if kind == "percent_of_recovery":
        return f"{tier.amount_max:g}% of costs" if tier.amount_max else "% of costs"
    if kind == "per_unit":
        base = f"{_fmt_money(tier.per_unit or 0)}/unit"
        if tier.cap:
            return f"{base}, cap {_fmt_money(tier.cap)}"
        return base
    if kind == "estimated_share":
        return f"~{_fmt_money(tier.per_unit or 0)}/share"
    if tier.amount_min is not None and tier.amount_max is not None \
            and tier.amount_min != tier.amount_max:
        return f"{_fmt_money(tier.amount_min)}–{_fmt_money(tier.amount_max)}"
    if kind == "range" and ceiling is not None:
        return f"Up to {_fmt_money(ceiling)}"
    return _fmt_money(ceiling) if ceiling is not None else "Varies"


@dataclass
class RowView:
    """Everything the template needs for one ledger row - precomputed."""

    id: str
    lane: str
    kind: str
    title: str
    url: str
    sticker: str
    payout_tier: str | None
    proof_badge: str
    proof_level: str | None
    deadline_prefix: str
    deadline_display: str
    deadline_iso: str
    days_left: int | None
    urgency: str  # "" | "soon" | "urgent"
    actionable: bool
    geo: str
    states: list[str] = field(default_factory=list)
    ev_display: str = "—"
    ev_confidence: str = ""
    # Each flag is (badge label, full reason, looks_like_a_caution). The badge
    # shows the short label; the tooltip carries the technical text, so
    # "flagged" is never shown without saying why.
    flags: list[tuple[str, str, bool]] = field(default_factory=list)
    claim_url: str | None = None
    official_website: str | None = None
    status_text: str | None = None
    warnings: list[str] = field(default_factory=list)
    xrefs: list[tuple[str, str]] = field(default_factory=list)


# The site shows the *reason*, not a bare "flagged": each internal warning
# phrase maps to a short human label, and the full text rides along as the
# badge's tooltip. Ordered, first match wins - like classification. The last
# element says whether the badge should look like a caution: provenance notes
# ("figures came from the case page") are normal operation, while missing
# proof rules or court-record links are things a claimant should know about.
_WARNING_BADGES: tuple[tuple[str, str, bool], ...] = (
    ("tiers derived from the page fact", "figures from case page", False),
    ("proof level differs from the index", "sources disagree on proof", True),
    ("no proof requirement published", "proof rules not published", True),
    ("no quick-facts block found", "details incomplete", True),
    ("differs from index", "deadline updated", False),
    ("source links a court record", "no settlement site found", True),
    # Verification-catalog conflicts. These markers are phrased after the
    # disagreement rather than after the source, so SettleSignal, ClaimDepot
    # and any future catalog all read the same and no source can ship a
    # sentence-length badge by being added later. The proof pair covers both
    # directions, because "their X; ours Y" reads differently depending on
    # which side demands proof.
    ("lists claim deadline", "sources disagree on deadline", True),
    ("says proof required; our sources say", "sources disagree on proof", True),
    ("says no proof needed; our sources say", "sources disagree on proof", True),
    ("but we have it in the", "sources disagree on status", True),
    ("but we show it as open", "listed closed elsewhere", True),
)


def _flag_from_warning(warning: str) -> tuple[str, str, bool]:
    for marker, label, caution in _WARNING_BADGES:
        if marker in warning:
            return label, warning, caution
    return warning, warning, True


def _same_site(a: str | None, b: str | None) -> bool:
    """Same destination, ignoring scheme-case and a trailing slash."""
    if not a or not b:
        return False
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def build_row(s: Settlement, *, today: date_cls, soon_days: int, urgent_days: int) -> RowView:
    days = None
    if s.deadline.date is not None:
        days = (s.deadline.date - today).days
    urgency = ""
    if days is not None and s.deadline.is_actionable() and days >= 0:
        if days <= urgent_days:
            urgency = "urgent"
        elif days <= soon_days:
            urgency = "soon"
    ev = "—" if s.ev_estimate is None else f"~{_fmt_money(s.ev_estimate)}"
    flags = [_flag_from_warning(w) for w in s.warnings]
    if str(s.kind) == "recall":
        flags.append(("recall remedy only", "the remedy is the recall program, not a claim form", True))
    if str(s.kind) == "investigation":
        flags.append(("nothing to claim yet", "an invitation to join the case, not a settlement", True))
    if str(s.deadline.kind) == "no_action":
        flags.append(("no deadline to miss", "no action deadline - payments happen without a claim", False))
    prefix = _DEADLINE_PREFIX.get(str(s.deadline.kind), "Deadline")
    # One destination, one link: most administrators run the claim portal and
    # the settlement site on the same domain (220 of 259 rows at time of
    # writing). Repeating it as "official site" halves the click target for
    # no new information - the surviving link is labeled "claim portal".
    official = s.official_website
    if _same_site(s.claim_url, official):
        official = None
    return RowView(
        id=s.id,
        lane=str(s.lane),
        kind=str(s.kind),
        title=s.title,
        url=s.source_url,
        sticker=money_sticker(s),
        payout_tier=s.payout_tier,
        proof_badge=_PROOF_BADGES.get(str(s.proof_level or ""), "Unknown"),
        proof_level=str(s.proof_level) if s.proof_level else None,
        deadline_prefix=prefix,
        deadline_display=_fmt_date(s.deadline.date),
        deadline_iso=s.deadline.date.isoformat() if s.deadline.date else "",
        days_left=days,
        urgency=urgency,
        actionable=s.deadline.is_actionable(),
        geo=geo_label(s),
        states=list(s.geo.states or []),
        ev_display=ev,
        ev_confidence=str(s.ev_confidence) if s.ev_estimate is not None else "",
        warnings=list(s.warnings),
        claim_url=s.claim_url,
        official_website=official,
        status_text=s.status_text,
        flags=flags,
        xrefs=[
            (_XREF_LABELS.get(name, name), refs[0]["href"])
            for name, refs in sorted(s.cross_refs.items())
            if refs and refs[0].get("href")
        ],
    )


_LANE_ORDER: dict[str, object] = {
    "claimable": lambda r: (r.days_left is None, r.days_left or 0, r.title.lower()),
    # Inaction has consequences on election/opt-out dates, so those rise to
    # the top; plain "no action needed" entries sink to the bottom.
    "automatic": lambda r: (
        0 if r.deadline_prefix in {"Election by", "Opt out by"} else 1,
        r.days_left is None,
        r.days_left if r.days_left is not None else 9999,
        r.title.lower(),
    ),
    "investigation": lambda r: r.title.lower(),
    "pending": lambda r: r.title.lower(),
}


def order_rows(rows: list[RowView]) -> dict[str, list[RowView]]:
    """Group rows by lane, each lane in its own urgency order."""
    lanes: dict[str, list[RowView]] = {name: [] for name in _LANE_ORDER}
    for row in rows:
        lanes.setdefault(row.lane, []).append(row)
    for name, bucket in lanes.items():
        bucket.sort(key=_LANE_ORDER[name])
    return lanes


@dataclass
class RunwayTick:
    pos_pct: float
    tier: str | None
    urgency: str
    title: str
    deadline_display: str
    days_left: int


@dataclass
class RunwayMonth:
    pos_pct: float
    label: str


def build_runway(
    rows: list[RowView],
    *,
    today: date_cls,
    horizon_days: int = 120,
) -> tuple[list[RunwayTick], list[RunwayMonth]]:
    """Ticks for every actionable deadline inside the horizon.

    Position is proportional to days remaining: the left edge is today, so
    ticks slide left as their windows close.
    """
    ticks: list[RunwayTick] = []
    for row in rows:
        if not row.actionable or row.days_left is None:
            continue
        if not 0 <= row.days_left <= horizon_days:
            continue
        ticks.append(
            RunwayTick(
                pos_pct=round(row.days_left / horizon_days * 100, 2),
                tier=row.payout_tier,
                urgency=row.urgency,
                title=row.title,
                deadline_display=f"{row.deadline_prefix} {row.deadline_display}",
                days_left=row.days_left,
            )
        )
    ticks.sort(key=lambda t: t.pos_pct)

    # Month gridlines: the first of each month inside the horizon.
    months: list[RunwayMonth] = []
    probe = today
    for _ in range(6):
        if probe != today and (probe - today).days <= horizon_days:
            months.append(
                RunwayMonth(
                    pos_pct=round((probe - today).days / horizon_days * 100, 2),
                    label=_MONTHS[probe.month - 1],
                )
            )
        if probe.month == 12:
            try:
                probe = probe.replace(year=probe.year + 1, month=1, day=1)
            except ValueError:
                break
        else:
            try:
                probe = probe.replace(month=probe.month + 1, day=1)
            except ValueError:
                break
    return ticks, months


_LANE_HEADINGS = {
    "claimable": ("Lane 1 · Claimable now", "Open claim windows, soonest deadline first."),
    "automatic": ("Lane 2 · Automatic", "No claim form — but watch election and opt-out dates."),
    "investigation": (
        "Lane 3 · Investigations to join",
        "Nothing to claim yet; a free case review may add you if this succeeds.",
    ),
    "pending": (
        "Lane 4 · Pending / not yet verified",
        "A settlement exists but its claim window is not verified. Do not act on these yet.",
    ),
}


def render_site(
    settlements: list[Settlement],
    out_dir: Path | str | None = None,
    *,
    today: date_cls | None = None,
    index_updated: str | None = None,
    dataset_path: Path | str | None = None,
    horizon_days: int = 120,
    soon_days: int = 7,
    urgent_days: int = 3,
) -> list[Path]:
    """Render docs/index.html and copy the static assets + dataset."""
    config = load_config()
    root = config.root
    out = Path(out_dir) if out_dir else root / config.paths.site_dir
    today = today or date_cls.today()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows = [
        build_row(s, today=today, soon_days=soon_days, urgent_days=urgent_days)
        for s in settlements
    ]
    lanes = order_rows(rows)
    runway_rows = [r for r in rows if r.lane in {"claimable", "automatic"}]
    ticks, months = build_runway(runway_rows, today=today, horizon_days=horizon_days)
    states = sorted({s for r in rows for s in r.states})

    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("index.html.j2").render(
        lane_headings=_LANE_HEADINGS,
        lanes=lanes,
        lane_names=list(_LANE_ORDER),
        ticks=ticks,
        months=months,
        horizon_days=horizon_days,
        states=states,
        total=len(rows),
        today_iso=today.isoformat(),
        today_display=_fmt_date(today),
        index_updated=index_updated,
        generated_at=generated_at,
    )
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    index_path = out / "index.html"
    index_path.write_text(html, encoding="utf-8")
    written.append(index_path)

    static_dir = _TEMPLATE_DIR / "static"
    if static_dir.is_dir():
        for asset in sorted(static_dir.iterdir()):
            if asset.is_file():
                shutil.copy2(asset, out / asset.name)
                written.append(out / asset.name)

    # GitHub Pages runs Jekyll over docs/ by default; the built site is
    # already finished HTML, so skip that pass (and its Liquid parsing).
    nojekyll = out / ".nojekyll"
    nojekyll.write_text("", encoding="utf-8")
    written.append(nojekyll)

    if dataset_path is None:
        candidate = root / config.paths.data_json
        dataset_path = candidate if candidate.exists() else None
    if dataset_path is not None and Path(dataset_path).exists():
        target = out / "data" / "settlements.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dataset_path, target)
        written.append(target)
    return written


def geo_label(settlement: Settlement) -> str:
    geo = settlement.geo
    if geo.label:
        return geo.label
    if geo.scope_type == "nationwide":
        return "Nationwide"
    if geo.scope_type == "states" and geo.states:
        return ", ".join(geo.states)
    if geo.scope_type == "country":
        return geo.country or "See details"
    return "—"
