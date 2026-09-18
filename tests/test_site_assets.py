"""The published site must be self-contained and shippable.

Regression test for a bug that only appeared once the site was deployed:
``index.html.j2`` linked ``style.css`` and ``app.js``, but nothing copied
them into ``docs/`` - so the live page was unstyled and its filters were
dead. Not one test looked at the output directory, so the build stayed
green while the deploy was broken. It also pins the packaging metadata, since
a wheel install with no template would fail the same way.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from kya.site_build import render_site

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "kya" / "templates"

_REF = re.compile(r'(?:href|src)="([^"]+)"')
_EXTERNAL = ("http://", "https://", "mailto:", "data:", "#", "//")


def nested_rule_lines(css: str) -> tuple[list[str], int]:
    """Find style rules nested inside other rules, plus the final depth.

    That nesting is the signature of a block left unclosed earlier in the
    file: every following rule, and the whole rest of the sheet, becomes
    nested instead of applying at the top level. Browsers recover silently
    (CSS nesting is legal) and instead match those rules only against
    descendants of the broken selector, so the page renders with almost none
    of its intended styling and reports no error at all. Counting braces
    cannot see it - the stray closing brace of a later rule balances the
    file - so the structure has to be walked.
    """
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    stack: list[bool] = []
    nested: list[str] = []
    for raw in stripped.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.count("{"):
            if stack and not stack[-1]:
                nested.append(line)
            stack.append(line.startswith("@"))
        for _ in range(line.count("}")):
            if stack:
                stack.pop()
    return nested, len(stack)


@pytest.fixture(scope="module")
def built(tmp_path_factory, settlements) -> Path:
    """Render the real site once, into a throwaway directory."""
    out = tmp_path_factory.mktemp("site")
    render_site(settlements, out_dir=out)
    return out


def test_every_referenced_asset_is_shipped(built: Path) -> None:
    html = (built / "index.html").read_text(encoding="utf-8")
    missing = []
    for ref in _REF.findall(html):
        if ref.startswith(_EXTERNAL):
            continue
        target = built / ref.split("#", 1)[0].split("?", 1)[0]
        if not target.is_file():
            missing.append(ref)
    assert not missing, f"the page references assets it does not ship: {sorted(set(missing))}"


def test_plausible_asset_references_were_found(built: Path) -> None:
    """Guard the test above against a template that stops linking anything."""
    html = (built / "index.html").read_text(encoding="utf-8")
    refs = {r for r in _REF.findall(html) if not r.startswith(_EXTERNAL)}
    assert {"style.css", "app.js", "favicon.svg"} <= refs


def test_static_assets_are_copied_and_non_empty(built: Path) -> None:
    for name in ("style.css", "app.js", "favicon.svg"):
        asset = built / name
        assert asset.is_file(), f"{name} was not copied into the site output"
        assert asset.stat().st_size > 0, f"{name} is empty"


def test_stylesheet_covers_the_documents_own_markup(built: Path) -> None:
    """Every class the template emits should exist in the stylesheet.

    Catches a rename on one side of the fence - markup that renders unstyled
    is invisible in HTML assertions but obvious in a browser.
    """
    html = (built / "index.html").read_text(encoding="utf-8")
    css = (built / "style.css").read_text(encoding="utf-8")
    used = set()
    for attr in re.findall(r'class="([^"]+)"', html):
        used.update(attr.split())
    # dynamic classes the builder appends (tick--s, row--urgent, sticker--tier-a)
    used = {c for c in used if c}
    uncovered = {c for c in used if f".{c}" not in css}
    assert not uncovered, f"markup classes with no style rule: {sorted(uncovered)}"


def test_github_pages_gets_a_nojekyll_marker(built: Path) -> None:
    assert (built / ".nojekyll").is_file()


def test_dataset_lands_where_the_page_links_it(built: Path) -> None:
    html = (built / "index.html").read_text(encoding="utf-8")
    assert 'href="data/settlements.json"' in html
    payload = json.loads((built / "data" / "settlements.json").read_text(encoding="utf-8"))
    assert payload["settlements"], "dataset shipped with no settlements"


def test_stylesheet_does_not_nest_rules(built: Path) -> None:
    """No rule may sit inside another rule: that means a block is unclosed."""
    css = (built / "style.css").read_text(encoding="utf-8")
    nested, depth = nested_rule_lines(css)
    assert not nested, f"rules nested inside rules (unclosed block above?): {nested}"
    assert depth == 0, "stylesheet ends with an unclosed rule"


def test_nesting_detector_catches_an_unclosed_rule() -> None:
    """Pin the detector itself against the bug it exists to catch.

    This is the sheet shape that shipped broken: the closing half of a rule
    went missing, so the next rule nested inside it and the page lost its
    styling without a single warning from the browser. A brace count stays
    balanced here, which is exactly why the detector walks the structure.
    """
    broken = (
        ".visually-hidden {\n"
        "  position: absolute;\n"
        "  width: 1px;\n"
        ".masthead {\n"
        "  border-bottom: 3px double red;\n"
        "}\n"
        "  padding: 0;\n"
        "}\n"
    )
    assert broken.count("{") == broken.count("}")
    nested, depth = nested_rule_lines(broken)
    assert nested == [".masthead {"]
    assert depth == 0


def test_nesting_detector_allows_at_rules_and_single_line_rules() -> None:
    """@media legitimately holds rules; one-liners must not false-positive."""
    css = "@media (max-width: 40rem) {\n  .row { grid-template-columns: 1fr; }\n}\n.badge { color: red; }\n"
    nested, depth = nested_rule_lines(css)
    assert nested == []
    assert depth == 0


def test_cross_references_render_as_labeled_links(tmp_path: Path, settlements) -> None:
    """A verified secondary source must reach the page, labeled by its kind.

    Cross-references only exist on settlements the live build matched, so a
    render of the captured fixtures never emits them - which is exactly how a
    dead template loop, or an unstyled link, would go unnoticed. The stylesheet
    invariant above is exercised here too: this markup is the only place
    ``row__xref`` appears.
    """
    enriched = [
        s.model_copy(
            update={
                "cross_refs": {
                    "news": [
                        {
                            "label": "in the news",
                            "href": "https://topclassactions.com/example/",
                            "source": "topclassactions",
                        }
                    ],
                    "docket": [
                        {
                            "label": "docket",
                            "href": "https://www.courtlistener.com/docket/1/example/",
                            "source": "courtlistener",
                        }
                    ],
                }
            }
        )
        for s in settlements
    ]
    out = tmp_path / "site"
    render_site(enriched, out_dir=out)
    html = (out / "index.html").read_text(encoding="utf-8")

    assert "https://topclassactions.com/example/" in html
    assert "https://www.courtlistener.com/docket/1/example/" in html
    assert ">in the news<" in html
    assert ">docket<" in html
    assert 'class="row__xref"' in html
    css = (out / "style.css").read_text(encoding="utf-8")
    assert ".row__xref" in css, "the xref link renders unstyled"


def test_packaging_metadata_ships_every_template_file() -> None:
    """package-data must match reality, or a wheel install has no template."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    globs = pyproject["tool"]["setuptools"]["package-data"]["kya"]
    packaged: set[Path] = set()
    for pattern in globs:
        packaged |= {
            p.relative_to(TEMPLATES) for p in TEMPLATES.glob(pattern.removeprefix("templates/"))
        }
    on_disk = {p.relative_to(TEMPLATES) for p in TEMPLATES.rglob("*") if p.is_file()}
    assert on_disk <= packaged, f"template files a wheel would drop: {sorted(on_disk - packaged)}"