"""Coverage report: run the normaliser over every payout string in the corpus.

Usage:  python tools/inspect_normalize.py [--all | --grep WORD]

Prints, for every index entry with payout text, the parsed tiers in a compact
form, and flags the two failure modes that matter:

* FAIL-EXTRACT  - payout text exists but no tier was produced
* FAIL-UNDISCLOSED - money amounts are visible in the text but the tier
  came back ``undisclosed`` (the number was dropped, not quoted)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kya.models import PayoutType, ProofLevel  # noqa: E402
from kya.normalize import normalize_payout, parse_money_amounts  # noqa: E402
from kya.sources.openclassactions_index import parse_index  # noqa: E402


def tier_summary(tier) -> str:
    bits = [str(tier.payout_type)]
    for name in ("amount_min", "amount_max", "per_unit", "cap", "percent"):
        value = getattr(tier, name, None)
        if value is not None:
            bits.append(f"{name}={value:g}")
    if tier.proof_level:
        bits.append(str(tier.proof_level))
    return " ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grep", default=None)
    args = ap.parse_args()

    text = (Path("tests/fixtures/openclassactions_llms.txt")).read_text(encoding="utf-8")
    doc = parse_index(text, source_url="https://openclassactions.com/llms.txt")

    stats = {
        "entries": 0,
        "with_payout_text": 0,
        "with_tiers": 0,
        "fail_extract": 0,
        "fail_undisclosed": 0,
        "multi_tier": 0,
    }
    payout_types: dict[str, int] = {}

    for entry in doc.entries:
        if args.grep and args.grep.lower() not in entry.slug:
            continue
        stats["entries"] += 1
        payout = (entry.payout_phrase or "").strip()
        if not payout:
            print(f"{entry.slug}: (no payout text)")
            continue
        stats["with_payout_text"] += 1

        tiers = normalize_payout(payout)
        if tiers:
            stats["with_tiers"] += 1
            stats["multi_tier"] += 1 if len(tiers) > 1 else 0
        else:
            stats["fail_extract"] += 1
            print(f"FAIL-EXTRACT  {entry.slug}: {payout!r}")
            continue

        visible = parse_money_amounts(payout)
        all_undisclosed = all(
            str(t.payout_type) == PayoutType.UNDISCLOSED.value for t in tiers
        )
        if visible and all_undisclosed:
            stats["fail_undisclosed"] += 1
            print(f"FAIL-UNDISCLOSED {entry.slug}: {payout!r} -> {visible}")

        flags = []
        if len(tiers) > 1:
            flags.append("multi")
        if any(str(t.proof_level) == ProofLevel.L4.value for t in tiers):
            flags.append("L4")
        suffix = f"  [{','.join(flags)}]" if flags else ""
        print(f"{entry.slug}{suffix}: {payout!r}")
        for tier in tiers:
            print(f"    -> {tier_summary(tier)}  conf={tier.confidence}")
            payout_types[str(tier.payout_type)] = (
                payout_types.get(str(tier.payout_type), 0) + 1
            )

    print()
    print(f"entries={stats['entries']} payout_text={stats['with_payout_text']} "
          f"tiered={stats['with_tiers']} multi-tier={stats['multi_tier']}")
    print(f"FAIL-EXTRACT={stats['fail_extract']} FAIL-UNDISCLOSED={stats['fail_undisclosed']}")
    if payout_types:
        print("payout types:", ", ".join(f"{k}={v}" for k, v in sorted(
            payout_types.items(), key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
