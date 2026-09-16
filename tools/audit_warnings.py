"""Audit the warnings on the built dataset, grouped by shape.

Warnings are deliberately never hidden - but a *systematic* warning means the
parser disagrees with the source in a pattern, not that 100 settlements are
individually odd. Grouping them by shape is how a real disagreement is told
apart from a bug in normalisation.
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "settlements.json"


def shape(warning: str) -> str:
    """Collapse a warning to its family, keeping the two compared levels."""
    match = re.match(r"proof level differs from the index \(index=(\S+?), page=(\S+)\)", warning)
    if match:
        return f"proof_differs index={match.group(1)} page={match.group(2)}"
    return re.sub(r"\d+", "N", warning)[:70]


def dump_shape(rows: list[dict], wanted: str) -> int:
    """Print every distinct detail line behind one warning shape."""
    seen: set[str] = set()
    for row in rows:
        for warning in row.get("warnings") or []:
            if shape(warning) != wanted:
                continue
            detail = row.get("proof_detail") or ""
            if detail in seen:
                continue
            seen.add(detail)
            print(f"\n[{len(seen)}] {row['id']}")
            print(f"     {detail}")
    print(f"\n{len(seen)} distinct details for {wanted}")
    return 0


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--dump":
        rows = json.loads(DATA.read_text(encoding="utf-8"))["settlements"]
        return dump_shape(rows, sys.argv[2])

    rows = json.loads(DATA.read_text(encoding="utf-8"))["settlements"]
    counts: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)

    for row in rows:
        for warning in row.get("warnings") or []:
            key = shape(warning)
            counts[key] += 1
            if len(examples[key]) < 3:
                examples[key].append(row["id"])

    total = sum(counts.values())
    print(f"{len(rows)} settlements, {total} warnings\n")
    for key, count in counts.most_common():
        print(f"{count:>4}  {key}")
        for slug in examples[key]:
            print(f"        e.g. {slug}")

    # The proof disagreements are the ones worth reading in full.
    print("\n-- proof disagreements in detail --")
    seen: set[str] = set()
    for row in rows:
        for warning in row.get("warnings") or []:
            if not warning.startswith("proof level differs"):
                continue
            key = shape(warning)
            if key in seen:
                continue
            seen.add(key)
            print(f"\n{row['id']}  ({key})")
            print(f"   proof_required={row.get('proof_required')} detail={row.get('proof_detail')!r}")
            print(f"   index_label={row.get('proof_label')!r}")
            for tier in row.get("tiers") or []:
                print(f"     tier {tier.get('proof_level')} {tier.get('label')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())