"""config.yaml and the dataclasses must agree, or the build dies at startup.

``_build`` refuses unknown keys on purpose - a typo in config.yaml should fail
loudly rather than silently do nothing - which means a new config section is
exactly the kind of change that can break every run at once. The shipped
``config.yaml`` is therefore loaded here as-is, and the defaults are pinned so
that a repository configured before a section existed keeps working.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kya.config import SourcesConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_the_shipped_config_loads_and_carries_the_secondary_source_knobs() -> None:
    config = load_config()
    assert config.root == ROOT
    assert config.sources.news_enabled is True
    assert config.sources.docket_top_cases == 25


def test_a_config_without_the_sources_section_still_loads(tmp_path: Path) -> None:
    """The section is additive: an older config.yaml must not fail the build."""
    (tmp_path / "config.yaml").write_text("user_agent: Someone/1.0\n", encoding="utf-8")
    config = load_config(tmp_path / "config.yaml", root=tmp_path)
    assert config.sources == SourcesConfig()
    assert config.sources.news_enabled is True


def test_a_typo_in_a_sources_key_fails_loudly(tmp_path: Path) -> None:
    """Silently ignoring the key would disable the phase without saying so."""
    (tmp_path / "config.yaml").write_text(
        "sources:\n  docket_top_case: 25\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="docket_top_case"):
        load_config(tmp_path / "config.yaml", root=tmp_path)