"""Command-line entry point for the settlement build pipeline.

Installed by pyproject as the ``kya`` console script; ``tools/build_dataset.py``
is a thin wrapper around this module so there is exactly one implementation.

Usage:
    kya                       # live index, no page enrichment
    kya --pages               # also enrich ~260 case pages (~1 request/s)
    kya --pages --site        # also render the static site into docs/
    kya --limit 5             # smoke run against the first entries only
    kya --whoami              # bot identity + chats that have messaged it
    kya --send-test-message   # one test message with Done / Not mine buttons
    kya --notify-digest       # diff against the stored snapshot and send the news
    kya --set-webhook URL     # register the Milestone C worker as the webhook
    kya --decisions           # print decisions recorded by the worker

Telegram credentials come from the environment or the repo .env (see
.env.example); they are never read from config.yaml, which is committed.
"""

from __future__ import annotations

import argparse

from kya.build import build_all
from kya.config import load_config
from kya.http import FetchError, PoliteClient
from kya.sources.openclassactions_index import parse_index
from kya.sources.openclassactions_page import parse_page
from kya.store import (
    connect,
    export_json,
    load_dataset_file,
    load_snapshot_file,
    save_settlements,
    write_snapshot_file,
)

INDEX_URL = "https://openclassactions.com/llms.txt"


def fetch_pages(client, entries, *, out=print) -> dict:
    """Fetch case pages; each entry may degrade to index-only.

    The polite client raises :class:`FetchError` when a request fails at the
    connection level (retries exhausted), and the first scheduled run proved a
    single flaky page will otherwise kill an unattended build mid-scrape. One
    dead page must cost one index-only settlement, never the whole run.
    """
    pages: dict = {}
    for i, entry in enumerate(entries, 1):
        result = None
        try:
            result = client.get(entry.url)
        except FetchError as exc:
            out(f"  page {entry.slug}: {exc}")
        if result is not None and result.ok:
            pages[entry.slug] = parse_page(result.text, entry.url)
        elif result is not None:
            out(f"  page {entry.slug}: HTTP {result.status_code}")
        if i % 25 == 0:
            out(f"  ...{i}/{len(entries)} pages")
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the class-action dataset and site.")
    parser.add_argument("--pages", action="store_true", help="enrich with case pages")
    parser.add_argument("--limit", type=int, default=None, help="smoke-run cap")
    parser.add_argument("--site", action="store_true", help="also render docs/")
    parser.add_argument(
        "--whoami", action="store_true", help="print bot identity and known chats, then exit"
    )
    parser.add_argument(
        "--send-test-message",
        action="store_true",
        help="send one test message with Done / Not mine buttons, then exit",
    )
    parser.add_argument(
        "--notify-digest",
        action="store_true",
        help="diff against the stored snapshot and send a Telegram digest",
    )
    parser.add_argument(
        "--set-webhook",
        metavar="URL",
        default=None,
        help="register the Milestone C worker (base URL) as Telegram's webhook",
    )
    parser.add_argument(
        "--decisions",
        action="store_true",
        help="print decisions recorded by the webhook worker, then exit",
    )
    args = parser.parse_args(argv)

    # --- webhook / decisions: self-contained Telegram worker calls -----------
    if args.set_webhook or args.decisions:
        from kya import notify

        try:
            if args.set_webhook:
                bot, _ = notify.bootstrap()
                return notify.cmd_set_webhook(bot, args.set_webhook)
            return notify.cmd_decisions()
        except notify.TelegramError as exc:
            print(f"telegram: {exc}")
            return 1

    # --- Telegram commands: no index fetch needed, exit before any scraping ---
    telegram_only = args.whoami or args.send_test_message or args.notify_digest
    if telegram_only:
        from kya import notify

        try:
            bot, chat_id = notify.bootstrap()
        except notify.TelegramError as exc:
            print(f"telegram: {exc}")
            return 1

        if args.whoami:
            return notify.cmd_whoami(bot)

        if args.send_test_message:
            if not chat_id:
                print(
                    "telegram: KYA_TELEGRAM_CHAT_ID is not set. Run `kya --whoami` "
                    "after messaging the bot, then put the chat id in .env."
                )
                return 1
            return notify.cmd_send_test_message(bot, chat_id)

        # --notify-digest: fall through to the build below, but remember to send.
        if not chat_id:
            print(
                "telegram: KYA_TELEGRAM_CHAT_ID is not set. Run `kya --whoami` "
                "after messaging the bot, then put the chat id in .env."
            )
            return 1
        send_digest = True
    else:
        send_digest = False

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
        pages = fetch_pages(client, entries)

    settlements = build_all(document, pages or None)

    # Secondary sources are additive: each phase degrades to a no-op, and a
    # cross-reference is attached only when the match verifies. See xref.py.
    from kya import xref

    settlements = xref.attach_cross_references(
        settlements,
        client,
        news_enabled=config.sources.news_enabled,
        docket_top_cases=config.sources.docket_top_cases,
        # Only the top cases are re-checked each run, so a case that slid out of
        # that window keeps the court record it already had. The previous
        # published dataset is the right source: it is committed, so it exists
        # in a fresh CI container where .state/ does not.
        previous=load_dataset_file(config.data_json_path),
    )

    by_lane: dict[str, int] = {}
    warned = 0
    for s in settlements:
        by_lane[str(s.lane)] = by_lane.get(str(s.lane), 0) + 1
        if s.warnings:
            warned += 1

    conn = connect(config.db_path)
    # The digest's baseline comes from the committed snapshot file, not from
    # SQLite: a scheduled run starts in a container with no .state/, and an
    # empty baseline means every case reads as new and the digest goes silent.
    previous_snapshot = load_snapshot_file(config.snapshot_path) if send_digest else None
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

    if send_digest:
        from kya import notify

        bot, chat_id = notify.bootstrap()
        status = notify.cmd_notify_digest(
            bot, chat_id, settlements, previous=previous_snapshot
        )
        if status != 0:
            return status
        # Recorded only after delivery succeeds: a failed notification must be
        # retried next run, not swallowed by a snapshot that says "already
        # reported". The file is committed alongside the dataset, so tomorrow's
        # run diffs against exactly what today's run published.
        snapshot = write_snapshot_file(config.snapshot_path, settlements)
        print(f"snapshot: {len(settlements)} cases -> {snapshot}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
