"""ClaimDepot - a third-party settlement directory, used to *verify*.

``claimdepot.com`` runs a Webflow directory of US settlements whose listing
cards are unusually well structured: every card carries a case status from a
fixed nine-value vocabulary, a claim deadline, a payout string and - on the
cards where it applies - a "No Proof" badge. That makes it a genuinely
independent third opinion on the two axes this project exists to answer,
alongside openclassactions and :mod:`kya.sources.settlesignal`.

Two upstream quirks are load-bearing, and both were found by reading captured
markup rather than by trusting field names:

* ``fs-cmsfilter-field`` is **not** a reliable field name. The days-left
  counter is tagged ``proof`` (and the card summary ``habitaciones``), so a
  scan for proof by that attribute finds a number and a "Days left" label.
  The real badge is the ``c-card_type`` element. The parser excludes the
  mislabelled counter from the positional values, because otherwise a card
  with no payout would hand its days remaining over as the payout string.
* Only ``No Proof`` is ever published as a badge. A card without one has
  *abstained* on proof, which is not the same as asserting proof is required,
  so a missing badge stays ``None`` and is never compared.

What this module does - and deliberately does not do:

* **It cross-checks.** Where a matched pair disagrees on the claim deadline,
  the proof requirement, or whether the case is still open, a warning is
  recorded and *ours is kept* - a conflict is information, not an error to
  auto-resolve. Same contract as :mod:`kya.xref` and SettleSignal.
* **It links.** The matched case page becomes a third ``cross_refs`` entry, so
  a reader can check our figures against an independent directory.
* **It does not import.** A card carries no official claim link, so an imported
  row would give the reader nothing to click - the one thing this site exists
  to provide. Discovery is already covered by SettleSignal, which attaches
  official links. Enriching from the detail pages instead would cost one fetch
  per case; that is a deliberate future option, not an oversight.

Matching is by *identity*, not by title similarity, and the calibration that
decided this is worth recording. Title matching fails both ways on real data:
it false-pairs (ClaimDepot's "Thinkware Dashcam $850,000" paired with our
"Dartmouth-Hitchcock $850,000 ERISA" on the shared dollar figure alone, at a
similarity of 0.33 - above any workable floor) and it misses (ClaimDepot's
"Alaska Airlines Military Leave" case against our row at 0.14, below every
floor that keeps the false pairs out). But a claimdepot.com card's URL slug is
the case's own settlement-website domain with the separators removed:
``/settlements/alaska-military-leave-settlement`` vs
``alaskamilitaryleavesettlement.com``. That was verified on every captured
detail page (16/16: the published "Settlement Website" always equals the slug
minus dots), and across the live listing it pairs 287 of our rows with zero
ambiguous cases - while title matching missed one true pair and invented
several false ones. So a pair is announced only when the identities are
*equal*, and an identity that appears on two rows of either side (a shared
portal such as ``forms.ksacms.com`` or a government host such as ``ftc.gov``)
is excluded entirely: a host serving several cases is not a case's identity.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Sequence

from kya.deadlines import extract_first_date
from kya.http import FetchError, PoliteClient
from kya.models import Lane, Settlement
from kya.textutil import collapse_ws

__all__ = [
    "ClaimDepotCard",
    "fetch_cards",
    "parse_listing_page",
    "parse_page_token",
    "identity_from_host",
    "identity_from_slug",
    "row_identities",
    "match_cards",
    "attach",
    "enrich",
]

LISTING_URL = "https://www.claimdepot.com/settlements"
# The page query token is a Webflow collection id and can change when the
# collection is rebuilt, so it is discovered from the pagination markup and
# only this value is the fallback.
PAGE_PARAM_FALLBACK = "0ff52671_page"
CARDS_PER_PAGE = 100
MAX_PAGES = 30  # the live listing is 27 pages; the cap is a runaway guard

# Public suffixes a settlement site's host may end in, used to strip the TLD
# before comparing a host to a card slug (see :func:`identity_from_host`).
# Compound suffixes must be checked before the simple set, because ``co.uk``
# would otherwise strip to the meaningless ``uk``.
_SIMPLE_SUFFIXES = frozenset({"com", "org", "net", "us", "io", "co", "ca", "info"})
_COMPOUND_SUFFIXES = frozenset({"co.uk", "com.au", "org.uk", "co.za", "com.br", "org.au"})

# Their status vocabulary, read off the sitemap's /case-status/ routes - a
# closed set of nine, which is what makes it usable as a comparable.
STATUS_LANES: dict[str, Lane] = {
    "open for claims": Lane.CLAIMABLE,
    "preliminarily approved": Lane.PENDING,
    "pending final approval": Lane.PENDING,
    "pending court approval": Lane.PENDING,
    "settlement approved": Lane.PENDING,
    "class certified": Lane.PENDING,
    "lawsuit filed": Lane.INVESTIGATION,
}
# History, not a lane: no opinion to compare against ours. They are still
# worth flagging when we show the same case as open.
CLOSED_STATUSES = frozenset({"closed", "paid"})

# Their proof vocabulary. "Not Applicable" is an abstention - most commonly
# seen on cases still awaiting approval - so it must not be read as "no proof
# needed". Mapping it to a level would invent a claim path that may not exist.
# "No Proof" is the easy path (self-report); "Proof Required" means documents.
PROOF_LABELS = ("Proof Required", "No Proof", "Not Applicable")
PROOF_LEVELS: dict[str, str] = {"No Proof": "L1", "Proof Required": "L3"}
PROOF_REQUIRED: dict[str, bool] = {"No Proof": False, "Proof Required": True}


@dataclass
class ClaimDepotCard:
    """One case card from the listing page."""

    slug: str
    title: str
    url: str
    status: str = ""
    payout: str = ""
    deadline_text: str = ""
    proof: str = ""

    @property
    def deadline_date(self) -> date | None:
        return extract_first_date(self.deadline_text)

    @property
    def lane(self) -> Lane | None:
        """Their lane, or ``None`` when the status is history we don't compare."""
        return STATUS_LANES.get(self.status.strip().lower())

    @property
    def proof_required(self) -> bool | None:
        """Their proof commitment; ``None`` for the "Not Applicable" abstention."""
        return PROOF_REQUIRED.get(self.proof)

    @property
    def proof_level(self) -> str | None:
        return PROOF_LEVELS.get(self.proof)


_CARD_ANCHOR_RE = re.compile(r'<a href="(/settlements/[^"?#]+)"[^>]*class="c-card_image')
_TITLE_RE = re.compile(r'class="c-title-3[^"]*"[^>]*>([^<]+)</a>')
# The status is the *first* ``category`` hook in card order; the second one is
# the subject area ("Labor", "Data Breach"). Position is load-bearing.
_STATUS_RE = re.compile(r'fs-cmsfilter-field="category"[^>]*>([^<]*)<')
# The proof badge is the only ``c-card_type`` element, and it is published only
# when proof is genuinely "No Proof". Its absence is an abstention, never a
# "proof required" - so this must stay a presence check, not a default.
_PROOF_BADGE_RE = re.compile(
    r'class="c-card_type"[^>]*>\s*<div[^>]*class="inner-card-text"[^>]*>([^<]*)<'
)
# ``fs-cmsfilter-field`` values, however, are *not* reliable field names: the
# days-left counter is tagged ``proof`` and the summary ``habitaciones``. The
# exclusion below keeps that mislabelled counter out of the positional values,
# so "57" / "Days left" can never be mistaken for a payout.
_TEXT_RE = re.compile(
    r'<div(?![^>]*fs-cmsfilter-field="proof")[^>]*class="c-text-2[^"]*"[^>]*>([^<]*)<'
)
_PAGE_TOKEN_RE = re.compile(r'href="\?([0-9A-Za-z]+_page)=\d+"')


def _clean(value: str) -> str:
    return collapse_ws(html.unescape(value))


def parse_page_token(page_html: str) -> str | None:
    """The Webflow pagination token, read from the "Next Page" anchor.

    Only present while a next page exists, which doubles as the stop signal.
    """
    match = _PAGE_TOKEN_RE.search(page_html)
    return match.group(1) if match else None


def parse_listing_page(
    page_html: str, *, base_url: str = "https://www.claimdepot.com"
) -> list[ClaimDepotCard]:
    """Every case card on one listing page.

    Cards are delimited by their own anchor rather than by a wrapper class,
    because each card's fields are read positionally within the card's slice -
    a card that grows a field must not be able to bleed into its neighbour.
    """
    anchors = list(_CARD_ANCHOR_RE.finditer(page_html))
    cards: list[ClaimDepotCard] = []
    for index, anchor in enumerate(anchors):
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(page_html)
        block = page_html[anchor.start() : end]

        title_match = _TITLE_RE.search(block)
        if not title_match:
            continue  # a card with no title cannot be matched to anything
        status_match = _STATUS_RE.search(block)
        status = _clean(status_match.group(1)) if status_match else ""

        values = [v for v in (_clean(raw) for raw in _TEXT_RE.findall(block)) if v]
        # Read from the badge element, not from ``values``: the badge is a
        # different class and never appears in the positional list, which is
        # exactly why a value-scan for proof silently found nothing.
        badge_match = _PROOF_BADGE_RE.search(block)
        proof = _clean(badge_match.group(1)) if badge_match else ""
        if proof not in PROOF_LABELS:
            proof = ""  # an unrecognised badge is not a commitment we can cite
        deadline_text = next(
            (v for v in values if extract_first_date(v) is not None), ""
        )
        consumed = {status, deadline_text}
        payout = next((v for v in values if v not in consumed), "")

        cards.append(
            ClaimDepotCard(
                slug=anchor.group(1).rsplit("/", 1)[-1],
                title=_clean(title_match.group(1)),
                url=f"{base_url}{anchor.group(1)}",
                status=status,
                payout=payout,
                deadline_text=deadline_text,
                proof=proof,
            )
        )
    return cards


def identity_from_host(url_or_host: str | None) -> str | None:
    """A host reduced to its case-identity form: no scheme, no ``www``, no dots.

    ``https://www.AlaskaMilitaryLeaveSettlement.com/`` becomes
    ``alaskamilitaryleavesettlement``. Returns ``None`` for anything without a
    parseable dotted host, so junk input simply never matches.
    """
    if not url_or_host:
        return None
    candidate = url_or_host.strip().lower()
    if "://" in candidate:
        candidate = candidate.split("://", 1)[1]
    candidate = candidate.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    candidate = candidate.rsplit("@", 1)[-1].split(":", 1)[0]
    if candidate.startswith("www."):
        candidate = candidate[len("www.") :]
    labels = [label for label in candidate.split(".") if label]
    if len(labels) < 2:
        return None
    if ".".join(labels[-2:]) in _COMPOUND_SUFFIXES and len(labels) > 2:
        labels = labels[:-2]
    elif labels[-1] in _SIMPLE_SUFFIXES:
        labels = labels[:-1]
    # Reduce to alphanumerics, exactly as :func:`identity_from_slug` does, so
    # ``weitz-br-settlement.com`` and the slug ``weitz-br-settlement`` meet in
    # the middle as ``weitzbrsettlement``.
    return re.sub(r"[^0-9a-z]+", "", "".join(labels)) or None


def identity_from_slug(slug: str) -> str:
    """A card slug in the same form as :func:`identity_from_host`.

    ``/settlements/Alaska-Military-Leave-Settlement`` becomes
    ``alaskamilitaryleavesettlement`` - which the captured detail pages prove
    is exactly the published Settlement Website domain minus dots.
    """
    return re.sub(r"[^0-9a-z]+", "", slug.lower())


def row_identities(settlement: Settlement) -> list[str]:
    """Every identity our row publishes: official site first, portal second.

    The two usually agree; where they differ both are offered, because a card
    may mirror either the settlement site or a dedicated claim domain.
    """
    identities: list[str] = []
    for url in (settlement.official_website, settlement.claim_url):
        ident = identity_from_host(url)
        if ident and ident not in identities:
            identities.append(ident)
    return identities


def match_cards(
    cards: Sequence[ClaimDepotCard],
    settlements: Sequence[Settlement],
) -> list[tuple[Settlement, ClaimDepotCard]]:
    """Pair cards with our rows by exact case identity, one-to-one.

    See the module docstring for why titles are not trusted with this. An
    identity claimed by two rows or two cards is dropped from *both* sides: a
    host that serves several cases - a shared claim-portal host or a government
    page - is not any single case's identity, and guessing would be exactly the
    false-pair mode title matching died of. One-to-one remains explicit so a
    row publishing two distinct domains cannot absorb two different cards.
    """
    row_of: dict[str, int] = {}
    shared: set[str] = set()
    for index, settlement in enumerate(settlements):
        for ident in row_identities(settlement):
            if ident in row_of:
                shared.add(ident)
            else:
                row_of[ident] = index
    for ident in shared:
        row_of.pop(ident, None)

    card_of: dict[str, int] = {}
    duplicated: set[str] = set()
    for index, card in enumerate(cards):
        ident = identity_from_slug(card.slug)
        if not ident:
            continue
        if ident in card_of:
            duplicated.add(ident)
        else:
            card_of[ident] = index
    for ident in duplicated:
        card_of.pop(ident, None)

    # Sorted iteration keeps the pairing identical across processes, and the
    # ``used_rows`` guard keeps the match one-to-one.
    pairs: list[tuple[Settlement, ClaimDepotCard]] = []
    used_rows: set[int] = set()
    for ident in sorted(card_of):
        row_index = row_of.get(ident)
        if row_index is not None and row_index not in used_rows:
            used_rows.add(row_index)
            pairs.append((settlements[row_index], cards[card_of[ident]]))
    return pairs


def fetch_cards(
    client: PoliteClient,
    *,
    max_pages: int = MAX_PAGES,
    out: Callable[[str], None] = print,
) -> list[ClaimDepotCard]:
    """Walk the listing pagination and return every card collected.

    A failure returns what has been gathered so far instead of raising: a
    verification source that is down must cost a cross-check, never a build.
    """
    cards: list[ClaimDepotCard] = []
    seen: set[str] = set()
    token: str | None = PAGE_PARAM_FALLBACK
    url: str | None = LISTING_URL
    page = 1
    while url and page <= max_pages:
        try:
            result = client.get(url)
        except (FetchError, OSError) as exc:
            out(f"claimdepot: {exc} - continuing without it")
            break
        if not result.ok:
            out(f"claimdepot: HTTP {result.status_code} on page {page}, stopping")
            break

        page_cards = parse_listing_page(result.text)
        if not page_cards:
            break  # an empty page means the listing ended, not that it broke

        # A page that re-reports anything already collected is not advancing,
        # and following it further would just re-fetch the same bytes to the
        # page cap. This is the guard against a wrong page-parameter name: an
        # unrecognised parameter is ignored by the server, which then serves
        # page one again for every request - silently duplicating the first
        # hundred cards instead of paginating. Stop rather than loop.
        fresh = [card for card in page_cards if card.slug not in seen]
        if page > 1 and not fresh:
            out(
                f"claimdepot: page {page} repeated known cards - pagination "
                "parameter may have changed; stopping"
            )
            break
        seen.update(card.slug for card in fresh)
        cards.extend(fresh)
        page_cards = fresh

        if len(page_cards) < CARDS_PER_PAGE:
            break  # a short page is the end of the listing

        # ``parse_page_token`` returns the *whole* parameter name, which
        # already ends in ``_page`` - so the placeholder carries the suffix,
        # not the template. Appending it here built ``?<token>_page=2`` and
        # the server ignored the doubled suffix.
        discovered = parse_page_token(result.text)
        if discovered:
            token = discovered
        if not token:
            break
        url = f"{LISTING_URL}?{token}={page + 1}"
        page += 1

    out(f"claimdepot: {len(cards)} card(s) over {page} page(s)")
    return cards


def _cross_check_one(settlement: Settlement, card: ClaimDepotCard) -> bool:
    """Compare one matched pair; ours wins every conflict.

    Returns ``True`` when a deadline conflict was recorded, so the caller can
    report the count. Only commitments are compared - a side that published
    nothing has abstained, not disagreed.
    """
    conflict = False

    theirs_date = card.deadline_date
    if settlement.deadline.date is not None and theirs_date is not None:
        if theirs_date != settlement.deadline.date:
            settlement.warnings.append(
                f"claimdepot lists claim deadline {theirs_date.isoformat()} but our "
                f"sources say {settlement.deadline.date.isoformat()}; ours kept"
            )
            conflict = True

    ours_proof = settlement.proof_required
    theirs_proof = card.proof_required
    # A dual-tier case is deliberately *not* compared on this axis. It carries
    # both a no-proof path and a documented one, so ``proof_required`` is True
    # while a reader can still claim without documents - CVS is the live
    # example, where the row-level level is L4 and the published label reads
    # "No proof required". Their "No Proof" badge is describing that easy path,
    # not contradicting us, so warning here would invent a conflict.
    # (SettleSignal sidesteps the identical shape by abstaining on its
    # "optional" value.) The row-level check matters because ``is_dual_tier``
    # reads the tier list, and a page-derived L4 row can hold only its easy
    # tier there.
    ours_is_dual = settlement.is_dual_tier() or str(settlement.proof_level or "") == "L4"
    if (
        ours_proof is not None
        and theirs_proof is not None
        and ours_proof != theirs_proof
        and not ours_is_dual
    ):
        settlement.warnings.append(
            "claimdepot says "
            f"{'proof required' if theirs_proof else 'no proof needed'}; our sources say "
            f"{'proof required' if ours_proof else 'no proof needed'}"
        )

    # Whether the case is open to claims *now* is the most actionable thing a
    # disagreement can touch - but only mismatches *within* the action axis
    # mean anything. Our lanes and their statuses are different axes: our lane
    # says what the reader must do, their status says where the case stands in
    # court. A case can be "Preliminarily Approved" while paying automatically,
    # and "Open for Claims" while some classes are paid without filing - 261 of
    # the first 286 live pairs disagreed exactly that way, and all of those
    # warnings were noise. So a lane conflict is recorded only when both sides
    # are speaking about case posture: claimable, pending or investigation.
    # Their "closed"/"paid" map to no lane at all: history we do not
    # second-guess, since we cannot tell a finished settlement from a stale
    # card - but they are still a claim about the reader's options, handled
    # below.
    theirs_lane = card.lane
    ours_lane = str(settlement.lane)
    # ``card.lane`` is a ``Lane`` enum member and ``str()`` of one is
    # ``"Lane.CLAIMABLE"``, never the plain value - the same footgun
    # :mod:`kya.models` works around with ``getattr(x, "value", x)``. Compare
    # plain values on both sides.
    theirs_lane_text = str(getattr(theirs_lane, "value", theirs_lane)) if theirs_lane else None
    _POSTURE_LANES = {
        Lane.CLAIMABLE.value,
        Lane.PENDING.value,
        Lane.INVESTIGATION.value,
    }
    if (
        theirs_lane_text is not None
        and theirs_lane_text != ours_lane
        and ours_lane in _POSTURE_LANES
        and theirs_lane_text in _POSTURE_LANES
    ):
        settlement.warnings.append(
            f"claimdepot lists this as '{card.status}' but we have it in the "
            f"'{settlement.lane}' lane"
        )
    elif card.status.strip().lower() in CLOSED_STATUSES and ours_lane in {
        Lane.CLAIMABLE.value,
        Lane.AUTOMATIC.value,
    }:
        # "Closed"/"paid" map to no lane, but they are still a claim about the
        # reader's options. If we show a case as open and an independent
        # directory has it finished, that is exactly the disagreement worth
        # surfacing: from here a stale card and a wrong window look identical,
        # so both are put in front of the reader rather than auto-resolved.
        settlement.warnings.append(
            f"claimdepot lists this as '{card.status}' but we show it as open "
            f"(the '{settlement.lane}' lane); check the claim deadline"
        )
    return conflict


def attach(
    settlements: Sequence[Settlement],
    cards: Sequence[ClaimDepotCard],
    *,
    out: Callable[[str], None] = print,
) -> list[Settlement]:
    """Cross-check every matched pair and attach the third-opinion link.

    The link is attached whether or not the pair agreed: the reader benefits
    from being able to check us either way, and a silent agreement is still a
    verification. What differs is the warnings, which are only written on a
    real conflict.
    """
    pairs = match_cards(cards, settlements)
    conflicts = 0
    for settlement, card in pairs:
        if _cross_check_one(settlement, card):
            conflicts += 1
        settlement.cross_refs.setdefault("claimdepot", []).append(
            {"label": "cross-checked", "href": card.url, "source": "claimdepot"}
        )
    out(
        f"claimdepot: {len(pairs)} case(s) cross-checked, "
        f"{conflicts} deadline conflict(s) recorded"
    )
    return list(settlements)


def enrich(
    settlements: Sequence[Settlement],
    client: PoliteClient,
    *,
    max_pages: int = MAX_PAGES,
    out: Callable[[str], None] = print,
) -> list[Settlement]:
    """Fetch the listing and attach cross-checks; never raises to the caller."""
    cards = fetch_cards(client, max_pages=max_pages, out=out)
    if not cards:
        return list(settlements)
    try:
        return attach(settlements, cards, out=out)
    except Exception as exc:  # noqa: BLE001 - a matching bug must not sink a build
        out(f"claimdepot matching failed: {exc}")
        return list(settlements)


