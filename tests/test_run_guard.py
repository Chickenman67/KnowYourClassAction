"""The build must refuse to replace a good dataset with a collapsed one.

The index is fetched with no schema to validate against, so a redesign, an A/B
variant or a CDN error page all arrive as HTTP 200 and parse to *zero* entries -
verified against the real parser. Nothing used to stand between that and the
published files, so a source-shape change would overwrite the dataset, blank the
live site, and reset the digest's baseline. The baseline is the silent part: an
empty snapshot means "establish one and stay quiet", so the next healthy run
would notify nobody about the cases that had disappeared.

These tests hold both halves: the decision function, and the fact that ``main``
consults it before writing anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kya import run as kya_run
from kya.config import load_config
from kya.http import FetchResult

BROKEN_INDEX = "<html><body>the site was redesigned</body></html>"
# Modelled on the real index: entries only count under the settlements heading,
# which is exactly the shape a redesign would break.
GOOD_INDEX = """\
# Open Class Actions

## Open Settlements — Claims Currently Open (Directory)

- [Kia Window Regulator Settlement](https://openclassactions.com/settlements/kia.php): Deadline: 2026-11-02.
- [Schuster Data Breach Settlement](https://openclassactions.com/settlements/schuster.php): Deadline: 2026-12-01.
"""


class StubClient:
    """Serves one canned index body; never touches the network."""

    def __init__(self, body: str):
        self.body = body
        self.fetched: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.fetched.append(url)
        return FetchResult(url=url, status_code=200, text=self.body)


def _entries(count: int) -> list[kya_run.IndexEntry]:
    return [
        kya_run.IndexEntry(
            url=f"https://openclassactions.com/settlements/case-{i}.php",
            title=f"Case {i} settlement",
            section="open",
        )
        for i in range(count)
    ]


def _published(count: int) -> dict[str, dict]:
    return {f"case-{i}": {"id": f"case-{i}"} for i in range(count)}


# --- the decision ---------------------------------------------------------------


def test_zero_entries_always_refuses() -> None:
    """The clearest signal there is: nothing parsed, so nothing is publishable."""
    assert kya_run._collapse_reason([], _published(273), allow_shrink=False)
    assert kya_run._collapse_reason([], _published(273), allow_shrink=True)


def test_a_collapse_against_a_large_dataset_refuses() -> None:
    reason = kya_run._collapse_reason(_entries(3), _published(273), allow_shrink=False)
    assert reason is not None
    assert "3 entries" in reason and "273" in reason


def test_a_healthy_growth_or_dip_is_published() -> None:
    """Cases do come and go; the guard is for collapse, not for change."""
    assert kya_run._collapse_reason(_entries(273), _published(269), allow_shrink=False) is None
    assert kya_run._collapse_reason(_entries(150), _published(273), allow_shrink=False) is None


def test_a_first_run_is_never_blocked() -> None:
    """No previous dataset means no comparison, not a refusal."""
    assert kya_run._collapse_reason(_entries(4), {}, allow_shrink=False) is None


def test_a_small_dataset_is_below_the_floor_where_proportion_means_anything() -> None:
    assert kya_run._collapse_reason(_entries(2), _published(10), allow_shrink=False) is None


def test_allow_shrink_is_the_deliberate_override() -> None:
    """A real shrink is possible, so the refusal has to be escapable."""
    assert kya_run._collapse_reason(_entries(3), _published(273), allow_shrink=True) is None


# --- the wiring ------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo root with a published dataset already in it."""
    (tmp_path / "config.yaml").write_text("user_agent: test/0.1\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    return tmp_path


def _run_main(monkeypatch, root: Path, body: str) -> tuple[int, StubClient]:
    config = load_config(root=root)
    client = StubClient(body)
    monkeypatch.setattr(kya_run, "load_config", lambda: config)
    monkeypatch.setattr(kya_run, "PoliteClient", lambda *a, **k: client)
    return kya_run.main([]), client


def _publish_dataset(root: Path, count: int) -> Path:
    target = root / "data" / "settlements.json"
    target.write_text(
        json.dumps({"count": count, "settlements": [{"id": f"case-{i}"} for i in range(count)]}),
        encoding="utf-8",
    )
    return target


def test_main_refuses_a_broken_index_without_touching_the_published_files(
    monkeypatch, repo: Path
) -> None:
    """The whole point: a source-shape change must not be able to publish."""
    dataset = _publish_dataset(repo, 273)
    before = dataset.read_text(encoding="utf-8")

    code, client = _run_main(monkeypatch, repo, BROKEN_INDEX)

    assert code == 1, "a collapsed build must fail the run, not publish"
    assert dataset.read_text(encoding="utf-8") == before, "the dataset was overwritten"
    assert not (repo / "data" / "snapshot.json").exists(), "the digest baseline was reset"
    assert len(client.fetched) == 1, "it should stop at the index, before scraping pages"


def test_main_publishes_a_healthy_index(monkeypatch, repo: Path) -> None:
    """The guard must not become a reason the tracker stops updating."""
    _publish_dataset(repo, 2)

    code, _ = _run_main(monkeypatch, repo, GOOD_INDEX)

    assert code == 0
    payload = json.loads((repo / "data" / "settlements.json").read_text(encoding="utf-8"))
    assert payload["count"] == 2


def test_main_refuses_a_collapse_that_is_not_merely_empty(
    monkeypatch, repo: Path
) -> None:
    """The zero-entry check alone would pass every other test in this file.

    An index that still parses, but to a fraction of what is published, is the
    likelier shape of a redesign - and unlike the empty case it is only caught
    if ``main`` really consults the guard with the comparison switched on.
    """
    dataset = _publish_dataset(repo, 273)
    before = dataset.read_text(encoding="utf-8")

    code, _ = _run_main(monkeypatch, repo, GOOD_INDEX)

    assert code == 1, "2 entries against 273 published must not be published"
    assert dataset.read_text(encoding="utf-8") == before


def test_a_smoke_run_is_not_subject_to_the_guard(monkeypatch, repo: Path) -> None:
    """--limit is meant to be tiny, so proportion cannot apply to it.

    Without this the guard would make every smoke run impossible, which is how
    a safety check gets deleted instead of fixed.
    """
    _publish_dataset(repo, 273)
    config = load_config(root=repo)
    monkeypatch.setattr(kya_run, "load_config", lambda: config)
    monkeypatch.setattr(kya_run, "PoliteClient", lambda *a, **k: StubClient(GOOD_INDEX))

    assert kya_run.main(["--limit", "1"]) == 0


def test_the_override_is_reachable_from_the_command_line(monkeypatch, repo: Path) -> None:
    """--allow-shrink is the documented escape hatch, so it has to exist."""
    _publish_dataset(repo, 273)
    config = load_config(root=repo)
    monkeypatch.setattr(kya_run, "load_config", lambda: config)
    monkeypatch.setattr(kya_run, "PoliteClient", lambda *a, **k: StubClient(GOOD_INDEX))

    assert kya_run.main(["--allow-shrink"]) == 0