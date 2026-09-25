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

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def test_readme_images_exist() -> None:
    """The README is the project's landing page, so its images must load.

    A screenshot it references is the first thing a visitor sees; a renamed or
    untracked file would silently render as a broken-image box on GitHub.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = _MD_IMAGE.findall(readme)
    assert targets, "the README should show what the tool looks like"
    missing = [t for t in targets if not t.startswith(_EXTERNAL) and not (ROOT / t).is_file()]
    assert not missing, f"README references images it does not ship: {missing}"



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


def test_tier_attribute_uses_bare_tier_values(built: Path) -> None:
    """``data-tier`` must hold S/A-F (or 'none'), never a rendered enum.

    The filter dropdown in ``app.js`` compares its option values against this
    attribute verbatim. ``{{ r.payout_tier }}`` renders a ``PayoutTier`` member
    through ``str()`` as ``PayoutTier.A``, so every tier option filtered every
    row out - while the sticker next to it stayed correct, because its
    ``|lower`` filter happens to call the *str method* and yields ``'a'``.
    """
    html = (built / "index.html").read_text(encoding="utf-8")
    tiers = set(re.findall(r'data-tier="([^"]*)"', html))
    assert tiers <= {"none", "S", "A", "B", "C", "D", "E", "F"}, tiers
    # And the dropdown's option values must line up with what rows carry.
    options = set(re.findall(r'<option[^>]*value="([^"]*)"', html))
    assert "none" in options
    assert tiers - {"none"} <= options, f"rows carry tiers the dropdown lacks: {tiers - {'none'} - options}"


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


def test_flagged_rows_say_why(built: Path, settlements) -> None:
    """A warning badge must carry its reason, never a bare word 'flagged'.

    The old build rendered ``flags`` as the literal string "flagged" and
    dropped the actual warning texts - 71 of 273 live rows said nothing but
    "flagged", which reads as alarm without information.
    """
    html = (built / "index.html").read_text(encoding="utf-8")
    assert ">flagged<" not in html
    for badge in re.findall(r'<span class="badge[^"]*">([^<]+)</span>', html):
        assert badge.strip() != "flagged", "an unexplained flag reached the page"
    # Every warn badge explains itself on hover.
    for badge in re.findall(r'<span class="badge[^"]*badge--warn[^"]*"[^>]*>', html):
        assert 'title="' in badge, f"caution badge without a reason: {badge}"

    # And the mapping is exercised: a settlement with a warning renders its
    # human label plus the full technical text in the tooltip.
    warned = [s.model_copy(update={"warnings": ["no proof requirement published"]}) for s in settlements[:1]]
    out = tmp_render(warned)
    page = (out / "index.html").read_text(encoding="utf-8")
    assert ">proof rules not published<" in page
    assert 'title="no proof requirement published"' in page


def tmp_render(rows, out_name: str = "site") -> Path:
    import tempfile

    out = Path(tempfile.mkdtemp()) / out_name
    render_site(rows, out_dir=out)
    return out


def test_duplicate_links_are_collapsed(built: Path) -> None:
    """One destination, one link inside a row.

    Most administrators run the claim portal and the settlement site on the
    same domain, so the old rows carried two links to one URL - halving the
    click target for no new information.
    """
    html = (built / "index.html").read_text(encoding="utf-8")
    for links in re.findall(r'<span class="row__links">(.*?)</span>', html, re.S):
        hrefs = re.findall(r'href="([^"]+)"', links)
        dupes = {h for h in hrefs if hrefs.count(h) > 1}
        assert not dupes, f"a row links the same URL twice: {dupes}"


def test_row_links_are_button_sized(built: Path) -> None:
    """The claim portal is the point of a row - it must not be 9pt text.

    Pins both halves of the contract: the template marks the primary link,
    and the stylesheet gives row links real padding (a tap target) instead
    of an underlined whisper.
    """
    html = (built / "index.html").read_text(encoding="utf-8")
    assert 'class="row__cta"' in html, "the claim portal lost its primary style"
    css = (built / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"\.row__links a \{(.*?)\}", css, re.S)
    assert rule, ".row__links a styling vanished from the stylesheet"
    assert "padding:" in rule.group(1) and "border:" in rule.group(1)
    assert ".row__cta" in css, ".row__cta is styled nowhere"


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