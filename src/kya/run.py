"""Command-line entry point for the settlement build pipeline.

Installed by pyproject as the ``kya`` console script; ``tools/build_dataset.py``
is a thin wrapper around this module so there is exactly one implementation.

Usage:
    kya                       # live index, no page enrichment
    kya --pages               # also enrich ~260 case pages (~1 request/s)
    kya --pages --site        # also render the static site into docs/
    kya --limit 5             # smoke run against the first entries only
"""

from __future__ import annotations

import argparse

from kya.build import build_all
from kya.config import load_config
from kya.http import PoliteClient
from kya.sources.openclassactions_index import parse_index
from kya.sources.openclassactions_page import parse_page
from kya.store import connect, export_json, save_settlements

INDEX_URL = "https://openclassactions.com/llms.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the class-action dataset and site.")
    parser.add_argument("--pages", action="store_true", help="enrich with case pages")
    parser.add_argument("--limit", type=int, default=None, help="smoke-run cap")
    parser.add_argument("--site", action="store_true", help="also render docs/")
    args = parser.parse_args(argv)

    config = load_config()
    client = PoliteClient(
        config.user_agent,
        timeout=config.http.timeout_seconds,
        min_delay=config.http.min_delay_seconds,
        max_retries=config.http.max_retries,
        cache_dir=config.cache_dir_path,
        respect_robots=config.http.respect_robots,
    )

    # get() serves a fresh cached copy when one exists (6h TTL) and falls
    # back to a stale copy if the network fails - so a rebuild never
    # requires a re-fetch and never loses a run to a transient error.
    index_result = client.get(INDEX_URL)
    if not index_result.ok:
        print(f"index fetch failed: HTTP {index_result.status_code}")
        return 1
    document = parse_index(index_result.text, source_url=INDEX_URL)
    entries = document.entries
    if args.limit:
        entries = entries[: args.limit]
        document.entries = entries
    print(f"index: {len(entries)} entries (last updated {document.last_updated})")

    pages = {}
    if args.pages:
        for i, entry in enumerate(entries, 1):
            result = client.get(entry.url)
            if result.ok:
                pages[entry.slug] = parse_page(result.text, entry.url)
            else:
                print(f"  page {entry.slug}: HTTP {result.status_code}")
            if i % 25 == 0:
                print(f"  ...{i}/{len(entries)} pages")

    settlements = build_all(document, pages or None)
    by_lane: dict[str, int] = {}
    warned = 0
    for s in settlements:
        by_lane[str(s.lane)] = by_lane.get(str(s.lane), 0) + 1
        if s.warnings:
            warned += 1

    conn = connect(config.db_path)
    written = save_settlements(conn, settlements)
    target = export_json(
        settlements,
        config.root / config.paths.data_json,
        index_updated=document.last_updated,
    )

    print(f"built: {len(settlements)} settlements -> {target}")
    print(f"lanes: {by_lane}")
    print(f"rows upserted: {written}; settlements with warnings: {warned}")

    if args.site:
        from kya.site_build import render_site

        outputs = render_site(
            settlements,
            index_updated=document.last_updated,
            soon_days=config.deadlines.soon_days,
            urgent_days=config.deadlines.urgent_days,
        )
        print(f"site: {len(outputs)} files -> {outputs[0].parent}")
    return 0
