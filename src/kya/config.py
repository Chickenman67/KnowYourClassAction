"""Configuration loading for KnowYourClassAction.

The repository root is discovered by walking up from this file until a
``pyproject.toml`` is found, so the tool behaves the same no matter which
directory it is invoked from (important for GitHub Actions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAME = "config.yaml"


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upwards looking for pyproject.toml; fall back to the cwd."""
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


@dataclass(frozen=True)
class HttpConfig:
    timeout_seconds: float = 25.0
    min_delay_seconds: float = 1.0
    max_retries: int = 3
    cache_dir: str = ".cache"
    cache_ttl_seconds: int = 21600
    respect_robots: bool = True


@dataclass(frozen=True)
class PathsConfig:
    data_json: str = "data/settlements.json"
    site_dir: str = "docs"
    db_path: str = ".state/kya.sqlite3"
    # The digest's memory of the previous build. Committed on purpose: the
    # SQLite query store is disposable and gitignored, so a scheduled run in a
    # fresh container would find it empty, treat every case as new, and take
    # the baseline path - the digest would never fire. Keeping it beside the
    # dataset it describes also makes each run's changes reviewable in git.
    snapshot_json: str = "data/snapshot.json"
    fixture_dir: str = "tests/fixtures"


@dataclass(frozen=True)
class LanesConfig:
    claimable: bool = True
    automatic: bool = True
    investigation: bool = True
    pending: bool = True

    def enabled(self) -> set[str]:
        return {
            name
            for name in ("claimable", "automatic", "investigation", "pending")
            if getattr(self, name)
        }


@dataclass(frozen=True)
class ScoringConfig:
    assumed_units_per_claim: int = 1
    pro_rata_assumed_share_pct: float = 0.0005


@dataclass(frozen=True)
class DeadlinesConfig:
    soon_days: int = 7
    urgent_days: int = 3
    final_days: int = 1


@dataclass(frozen=True)
class SourcesConfig:
    """Secondary-source cross-checks (``kya.xref``).

    Both phases degrade to no-ops on failure; these knobs only bound how much
    they attempt, never whether the build survives them.
    """

    news_enabled: bool = True
    docket_top_cases: int = 25
    # SettleSignal (settlesignal.com) - a free CC-BY 4.0 settlement catalog
    # used as an independent cross-check, and optionally as a discovery
    # source for cases no other feed carries. Both phases degrade to no-ops.
    settlesignal_enabled: bool = True
    settlesignal_import: bool = True


@dataclass(frozen=True)
class Config:
    root: Path
    user_agent: str
    http: HttpConfig = field(default_factory=HttpConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    lanes: LanesConfig = field(default_factory=LanesConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    deadlines: DeadlinesConfig = field(default_factory=DeadlinesConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)

    # --- resolved paths -------------------------------------------------
    def path(self, relative: str) -> Path:
        """Resolve a configured relative path against the repository root."""
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else (self.root / candidate)

    @property
    def data_json_path(self) -> Path:
        return self.path(self.paths.data_json)

    @property
    def site_dir_path(self) -> Path:
        return self.path(self.paths.site_dir)

    @property
    def db_path(self) -> Path:
        return self.path(self.paths.db_path)

    @property
    def snapshot_path(self) -> Path:
        return self.path(self.paths.snapshot_json)

    @property
    def fixture_dir_path(self) -> Path:
        return self.path(self.paths.fixture_dir)

    @property
    def cache_dir_path(self) -> Path:
        return self.path(self.http.cache_dir)


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError(f"config section '{key}' must be a mapping")
    return value


def _build(cls, data: dict[str, Any]):
    """Instantiate a frozen dataclass from a mapping, ignoring unknown keys is NOT
    allowed - a typo in config.yaml should fail loudly rather than silently."""
    allowed = set(cls.__dataclass_fields__)
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys in config.yaml: {sorted(unknown)}")
    return cls(**{k: v for k, v in data.items() if k in allowed})


DEFAULT_USER_AGENT = (
    "KnowYourClassAction/0.1 (+https://github.com/Chickenman67/KnowYourClassAction)"
)


def load_config(path: Path | str | None = None, root: Path | None = None) -> Config:
    root = root or find_repo_root()
    config_path = Path(path) if path else root / DEFAULT_CONFIG_NAME
    if not config_path.is_absolute():
        config_path = root / config_path

    raw: dict[str, Any] = {}
    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")

    return Config(
        root=root,
        user_agent=raw.get("user_agent") or DEFAULT_USER_AGENT,
        http=_build(HttpConfig, _section(raw, "http")),
        paths=_build(PathsConfig, _section(raw, "paths")),
        lanes=_build(LanesConfig, _section(raw, "lanes")),
        scoring=_build(ScoringConfig, _section(raw, "scoring")),
        deadlines=_build(DeadlinesConfig, _section(raw, "deadlines")),
        sources=_build(SourcesConfig, _section(raw, "sources")),
    )
