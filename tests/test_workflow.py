"""The scheduled workflow's invariants, pinned because they fail *silently*.

None of this is enforced by application code - it lives in YAML - and every
item here breaks in a way that leaves the build green: notifications duplicate,
or quietly stop, or the digest reports changes that never happened. That is
exactly the class of bug a test has to hold, because nothing else will.

The concurrency test is the one that earns its keep. ``cancel-in-progress:
true`` looks like the obvious fix for "two runs shouldn't overlap" and is what
a reasonable person would reach for - I did. It would break the digest: a
cancelled run may have already sent its notification without committing the
snapshot, so its replacement re-reads the old baseline and notifies the same
cases a second time. Serializing is what keeps notifications single.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"

# Every one of these silently disables notifications when it goes missing: the
# digest prints "not set" and exits 0, so the run still succeeds.
_REQUISITE_ENV = (
    "KYA_TELEGRAM_BOT_TOKEN",
    "KYA_TELEGRAM_CHAT_ID",
    "KYA_TELEGRAM_WEBHOOK_SECRET",
    "KYA_DECISIONS_URL",
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def triggers(workflow: dict) -> dict:
    """The ``on:`` block.

    ``on`` is a YAML 1.1 boolean, so PyYAML returns the key ``True`` rather
    than the string ``"on"``. Both spellings are accepted so the test does not
    depend on that quirk persisting.
    """
    block = workflow.get("on", workflow.get(True))
    assert isinstance(block, dict), "the workflow has no trigger block"
    return block


def _run_commands(workflow: dict) -> list[str]:
    return [step["run"] for step in workflow["jobs"]["build"]["steps"] if "run" in step]


def test_the_daily_schedule_exists_and_is_off_the_hour(triggers: dict) -> None:
    crons = [entry["cron"] for entry in triggers.get("schedule") or []]
    assert len(crons) == 1, f"expected exactly one schedule, found {crons}"
    minute, hour = crons[0].split()[:2]
    assert minute != "0", "an on-the-hour cron competes with everyone else's"
    assert "*" not in minute and "*" not in hour, f"{crons[0]} is not a daily run"


def test_it_can_still_be_run_by_hand(triggers: dict) -> None:
    assert "workflow_dispatch" in triggers


def test_there_is_no_push_trigger(triggers: dict) -> None:
    """Deliberate: every run commits data/ and docs/.

    With a push trigger, each hand-edit of the dataset would start a full index
    scrape, and the workflow's own commit would be trying to trigger the thing
    that made it. Rebuilds belong to the schedule and to ``workflow_dispatch``.
    """
    assert "push" not in triggers, "a push trigger would scrape the index on every commit"


def test_the_digest_runs_in_a_single_invocation(workflow: dict) -> None:
    """One build per run, and the digest shares it.

    The digest diffs against the stored snapshot *and rewrites it*, so a second
    build in the same run would compare against a snapshot it had just
    overwritten - manufacturing diffs that never happened - and would publish a
    site built from a different dataset than the one notified.
    """
    digests = [cmd for cmd in _run_commands(workflow) if "--notify-digest" in cmd]
    assert len(digests) == 1, f"expected one digest invocation, found {len(digests)}"
    digest = digests[0]
    assert "--pages" in digest, "the digest must run over the enriched dataset"
    assert "--site" in digest


def test_builds_serialize_instead_of_cancelling(workflow: dict) -> None:
    """The notification-correctness invariant - see this module's docstring."""
    concurrency = workflow["concurrency"]
    assert concurrency["group"], "runs need one shared concurrency group"
    assert concurrency.get("cancel-in-progress") is False, (
        "cancel-in-progress must stay false: cancelling a run that already sent "
        "its digest, before it commits the snapshot, makes the replacement "
        "notify the same cases again"
    )


def test_the_digest_credentials_are_wired_from_secrets(workflow: dict) -> None:
    """A dropped secret is invisible: the digest skips and the run stays green.

    ``KYA_COURTLISTENER_TOKEN`` is deliberately absent from this list - it is
    optional, and the build runs anonymously without it.
    """
    build = next(s for s in workflow["jobs"]["build"]["steps"] if "--notify-digest" in s.get("run", ""))
    env = build.get("env") or {}
    missing = [name for name in _REQUISITE_ENV if name not in env]
    assert not missing, f"the digest step is missing credentials: {missing}"
    unwired = [name for name in _REQUISITE_ENV if "secrets." not in str(env[name])]
    assert not unwired, f"these must come from secrets, not literals: {unwired}"


def test_it_may_commit_and_deploy(workflow: dict) -> None:
    """The run pushes docs/ and data/, which is also the Pages deploy."""
    assert workflow["permissions"]["contents"] == "write"