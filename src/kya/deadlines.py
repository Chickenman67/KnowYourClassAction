"""Turn upstream deadline text into a typed, honest :class:`Deadline`.

Upstream writes deadlines in at least nine shapes, and two of them mean the
*opposite* of "act now":

* ``No Claim Form (Automatic Payment)`` - there is nothing to do at all
* ``Rolling - no end date announced`` - there is no expiry to race

Treating either as an ordinary dated deadline manufactures false urgency.
Worse, treating an **opt-out** date as a **claim** date is actively wrong: they
are different actions with opposite consequences (file to get paid vs. leave
the class to sue separately).

Dates are parsed with an explicit month table rather than a fuzzy date parser,
because fuzzy parsing turns "Under 35,000 miles" and "$1,718" into dates.
"""

from __future__ import annotations

import datetime
import re

from .models import Deadline, DeadlineKind
from .textutil import collapse_ws

__all__ = ["parse_deadline", "extract_first_date", "days_remaining"]

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# "November 23, 2026" / "Sept 14, 2026" / "Nov. 5, 2026"
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b"
)
_ISO_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")

_YEAR_FLOOR, _YEAR_CEIL = 2000, 2100


def extract_first_date(text: str | None) -> datetime.date | None:
    """Pull the first plausible calendar date out of a messy string.

    Only month-name and ISO forms are accepted, so dollar amounts, mileages and
    percentages cannot be mistaken for dates.
    """
    text = collapse_ws(text)
    if not text:
        return None
    for match in _MONTH_DAY_YEAR_RE.finditer(text):
        month = _MONTHS.get(match.group("month").lower())
        if month is None:
            continue
        try:
            candidate = datetime.date(
                int(match.group("year")), month, int(match.group("day"))
            )
        except ValueError:
            continue
        if _YEAR_FLOOR <= candidate.year <= _YEAR_CEIL:
            return candidate
    for match in _ISO_RE.finditer(text):
        try:
            candidate = datetime.date(
                int(match.group("year")), int(match.group("month")), int(match.group("day"))
            )
        except ValueError:
            continue
        if _YEAR_FLOOR <= candidate.year <= _YEAR_CEIL:
            return candidate
    return None


def _detect_kind(text: str) -> DeadlineKind:
    """Order matters: the earliest matching rule wins."""
    low = text.lower()

    # 1. Nothing to do / nothing to file.
    if "no claim form" in low or "automatic payment" in low:
        return DeadlineKind.NO_ACTION
    # 2. An open-ended window.
    if "rolling" in low:
        return DeadlineKind.ROLLING
    # 3. Genuinely unknown yet.
    if "not yet verified" in low or "pending final approval" in low or "not yet known" in low:
        return DeadlineKind.NOT_YET_KNOWN
    # 4. Objecting to the settlement (stays in the class, contests the terms).
    if "object by" in low or "objection" in low:
        return DeadlineKind.OBJECTION
    # 5. Leaving the class to sue separately.
    if "opt out" in low or "opt-out" in low or "exclusion" in low:
        return DeadlineKind.OPT_OUT
    # 6. A choice between payment routes, not a claim.
    if "optional election" in low or "payment-election" in low or "optional payment" in low:
        return DeadlineKind.OPTIONAL_ELECTION
    # 7. A date that only exists relative to an approval that has not happened.
    if "within" in low and "day" in low:
        return DeadlineKind.CONDITIONAL
    if "see claim form" in low or "when open" in low:
        return DeadlineKind.CONDITIONAL
    return DeadlineKind.UNPARSED


def parse_deadline(raw: str | None) -> Deadline:
    """Parse upstream deadline text into a typed deadline."""
    text = collapse_ws(raw)
    if not text:
        return Deadline(raw="", kind=DeadlineKind.UNPARSED)

    kind = _detect_kind(text)
    date = extract_first_date(text)

    # A recognisable date inside an otherwise ordinary deadline makes it a claim
    # deadline. A "within N days" clause is conditional even if a date follows.
    if kind is DeadlineKind.UNPARSED and date is not None:
        kind = DeadlineKind.CLAIM

    note: str | None = None
    if kind is DeadlineKind.ROLLING:
        note = text
    elif kind is DeadlineKind.NO_ACTION:
        note = "No claim form to file."
    elif kind is DeadlineKind.NOT_YET_KNOWN:
        note = "The claim window has not been verified yet."

    return Deadline(raw=text, kind=kind, date=date, note=note)


def days_remaining(deadline: Deadline, today: datetime.date | None = None) -> int | None:
    """Whole days left before an actionable date. ``None`` when there is none.

    A deadline that has already passed returns a negative number rather than
    silently disappearing.
    """
    if deadline.date is None or not deadline.is_actionable():
        return None
    today = today or datetime.date.today()
    return (deadline.date - today).days