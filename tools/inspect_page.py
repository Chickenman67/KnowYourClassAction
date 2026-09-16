"""Dump what the page parser extracted from the captured case pages.

The companion to ``inspect_index.py``: if the index gives discovery, this shows
whether enrichment actually recovered the payout and proof details that the
index omits for a quarter of entries.

Usage:
    python tools/inspect_page.py                 # all captured pages
    python tools/inspect_page.py --name kia      # one page
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kya.config import load_config  # noqa: E402
from kya.sources.openclassactions_page import parse_page  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", help="only pages whose fixture name contains this")
    args = parser.parse_args(argv)

    config = load_config()
    fixtures = config.fixture_dir_path
    pages = sorted(fixtures.glob("page_*.html"))
    if not pages:
        print(f"no page fixtures in {fixtures}\nrun: python tools/capture_fixtures.py")
        return 1

    for path in pages:
        if args.name and args.name.lower() not in path.name.lower():
            continue
        page = parse_page(path.read_text(encoding="utf-8"), url=path.name)
        print("=" * 78)
        print(path.name)
        print(f"  title      : {page.title}")
        print(f"  category   : {page.category}    breadcrumb={page.breadcrumb}")
        print(f"  date mod   : {page.date_modified}")
        print(f"  claim url  : {page.claim_url}")
        print("  facts:")
        for label, fact in page.facts.items():
            sub = f"   ||  {fact.sub[:70]}" if fact.sub else ""
            print(f"    {label:<24} = {fact.value[:70]}{sub}")
        print("  details:")
        for label, value in page.details.values.items():
            link = page.details.links.get(label)
            suffix = f"   -> {link}" if link else ""
            print(f"    {label:<24} = {value[:60]}{suffix}")
        print(f"  faq pairs  : {len(page.faq)}")
        if page.warnings:
            print(f"  warnings   : {page.warnings}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())