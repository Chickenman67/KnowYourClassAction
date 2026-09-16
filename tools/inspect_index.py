"""Dump what the index parser actually extracted from the captured fixture.

A development aid, and the fastest way to spot a vocabulary change upstream:
if upstream adds a new proof label, it shows up here as "unmapped" instead of
silently becoming a geo filter or a note.

Usage:
    python tools/inspect_index.py
    python tools/inspect_index.py --grep kia
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kya.config import load_config  # noqa: E402
from kya.sources.openclassactions_index import parse_index  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grep", help="only show entries whose title/url matches")
    parser.add_argument("--proof", help="only show entries at this proof level (L0..L4)")
    args = parser.parse_args(argv)

    config = load_config()
    fixture = config.fixture_dir_path / "openclassactions_llms.txt"
    if not fixture.is_file():
        print(f"missing fixture: {fixture}\nrun: python tools/capture_fixtures.py")
        return 1

    document = parse_index(fixture.read_text(encoding="utf-8"), source_url=str(fixture))

    print(f"index last updated : {document.last_updated}")
    print(f"entries            : {len(document)}")
    print(f"  open             : {len(document.open_entries())}")
    print(f"  unverified       : {len(document.unverified_entries())}")

    proof = collections.Counter(e.proof_level or "(unlabelled)" for e in document.entries)
    print("\nproof level:")
    for level, count in sorted(proof.items()):
        print(f"  {level:<14} {count}")

    geo = collections.Counter(
        (e.geo.scope_type if e.geo else "none") for e in document.entries
    )
    print("\ngeo scope type:")
    for kind, count in sorted(geo.items()):
        print(f"  {kind:<14} {count}")

    restricted = [e for e in document.entries if e.geo and e.geo.is_restricted()]
    print(f"\ngeo-restricted entries: {len(restricted)}")
    for entry in restricted[:12]:
        label = entry.geo.label if entry.geo else ""
        print(f"  {entry.geo.scope_type:<8} {entry.geo.states or entry.geo.country} :: {label[:70]}")

    investigations = [e for e in document.entries if e.is_investigation]
    print(f"\ninvestigation-labelled entries: {len(investigations)}")
    for entry in investigations:
        print(f"  {entry.title[:80]}")

    notes = collections.Counter(note for e in document.entries for note in e.notes)
    print("\ntop notes:")
    for note, count in notes.most_common(12):
        print(f"  {count:>3}  {note[:90]}")

    if args.grep or args.proof:
        print("\nselected entries:")
        for entry in document.entries:
            haystack = f"{entry.title} {entry.url}".lower()
            if args.grep and args.grep.lower() not in haystack:
                continue
            if args.proof and (entry.proof_level or "") != args.proof:
                continue
            print(f"  title      : {entry.title}")
            print(f"  case_name  : {entry.case_name}")
            print(f"  payout     : {entry.payout_phrase}")
            print(f"  deadline   : {entry.deadline_raw}")
            print(f"  proof      : {entry.proof_level} ({entry.proof_label_raw})")
            print(f"  geo        : {entry.geo.scope_type} {entry.geo.states} {entry.geo.label}")
            print(f"  notes      : {entry.notes}")
            print(f"  section    : {entry.section}  investigation={entry.is_investigation}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())