"""Capture real upstream payloads into ``tests/fixtures/``.

Parsers here are built against ground truth, not against assumptions about what
the upstream HTML looks like. Re-run this whenever the upstream layout changes.
A manifest records the exact URL, timestamp and SHA-256 of every fixture, so a
test failure can always be traced back to the exact bytes it ran against.

Usage:
    python tools/capture_fixtures.py            # capture everything
    python tools/capture_fixtures.py --list     # show what would be captured
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kya.config import load_config  # noqa: E402
from kya.http import FetchError, PoliteClient  # noqa: E402

BASE = "https://openclassactions.com"

# The machine-readable index: 260 open settlements, each already labelled with a
# deadline and a proof requirement. This is the spine of the whole project.
INDEX = ("openclassactions_llms.txt", f"{BASE}/llms.txt")

ROBOTS = ("openclassactions_robots.txt", f"{BASE}/robots.txt")

# Hand-picked detail pages covering every shape the parser has to survive:
# each entry is (fixture name, path, why it is interesting).
PAGES: list[tuple[str, str, str]] = [
    (
        "page_kia_window_regulator",
        "settlements/kia-window-regulator-class-action-settlement.php",
        "L3 documentation; payout is 'per Repair' with a cap; fund undisclosed",
    ),
    (
        "page_schuster_data_breach",
        "settlements/data-breaches/schuster-data-breach-class-action-settlement.php",
        "no payout phrase in the index but a real payout on the page; L2 notice ID + PIN",
    ),
    (
        "page_cvs_digital_privacy",
        "settlements/cvs-digital-privacy-class-action-settlement.php",
        "textbook dual tier: up to $5 with no proof, up to $10 with documentation",
    ),
    (
        "page_country_bank_overdraft",
        "settlements/country-bank-savings-overdraft-nsf-fee-settlement.php",
        "L0 automatic payment, no claim form at all",
    ),
    (
        "page_hearthside_child_labor",
        "settlements/hearthside-illinois-child-labor-assurance.php",
        "no proof label published upstream at all (the 13 unlabelled entries)",
    ),
    (
        "page_diisocyanates_antitrust",
        "settlements/diisocyanates-mdi-tdi-antitrust-class-action-settlement.php",
        "multi-defendant deadline envelope: 'October 13, 2026 (Wanhua)'",
    ),
    (
        "page_equifax_credit_score",
        "settlements/equifax-credit-score-error-class-action-settlement.php",
        "pending final approval: must land in the 'pending' lane, not claimable",
    ),
    (
        "page_ryobi_mower_recall",
        "lawsuits/product-liability/ryobi-40v-mower-fire-class-action-lawsuit.php",
        "an investigation/recall under /lawsuits/: must land in the investigation lane",
    ),
]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_fixture(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list targets and exit")
    args = parser.parse_args(argv)

    config = load_config()
    fixtures = config.fixture_dir_path

    targets = [INDEX, ROBOTS] + [
        (name, f"{BASE}/{path}") for name, path, _reason in PAGES
    ]

    if args.list:
        for name, url in targets:
            print(f"{name:38s} {url}")
        return 0

    manifest_path = fixtures / "MANIFEST.json"
    manifest: dict[str, dict[str, object]] = {}
    failures: list[str] = []

    with PoliteClient(
        config.user_agent,
        timeout=config.http.timeout_seconds,
        min_delay=config.http.min_delay_seconds,
        max_retries=config.http.max_retries,
        cache_dir=config.cache_dir_path,
        cache_ttl=0,  # always re-fetch: a fixture must be current
        respect_robots=config.http.respect_robots,
    ) as client:
        for name, url in targets:
            # Only append an extension when the fixture name does not already carry one.
            if name.endswith((".txt", ".html", ".xml", ".json")):
                suffix = ""
            elif url.endswith(("llms.txt", "robots.txt")):
                suffix = ".txt"
            else:
                suffix = ".html"
            try:
                result = client.get(url)
            except FetchError as exc:
                failures.append(f"{name}: {exc}")
                print(f"  FAIL  {name}  {exc}")
                continue
            write_fixture(fixtures / f"{name}{suffix}", result.text)
            manifest[name] = {
                "url": url,
                "file": f"{name}{suffix}",
                "status": result.status_code,
                "bytes": len(result.text),
                "sha256_16": sha256(result.text),
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(result.fetched_at)),
            }
            print(f"  ok    {name:34s} {result.status_code}  {len(result.text):>7,} bytes")

    existing = {}
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}
    existing.update(manifest)
    manifest_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")

    print(f"\n{len(manifest)} fixture(s) written to {fixtures}")
    if failures:
        print(f"{len(failures)} failure(s):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())