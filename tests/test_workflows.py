"""Static checks of the GitHub Actions workflows (no network, no Actions run)."""

from __future__ import annotations

import re

import pytest
import yaml

from conftest import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(wf: dict) -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True))


def steps(wf: dict) -> list[dict]:
    (job,) = wf["jobs"].values()
    return job["steps"]


@pytest.mark.parametrize("name", ["daily-review.yml", "crawl-check.yml"])
def test_actions_run_on_node24_versions(name):
    uses = [s["uses"] for s in steps(load(name)) if "uses" in s]
    assert uses, "no actions found"
    for u in uses:
        action, _, version = u.partition("@")
        major = int(re.match(r"v(\d+)", version).group(1))
        if action == "actions/checkout":
            assert major >= 5, u  # v4 runs on the deprecated Node 20
        if action == "actions/setup-python":
            assert major >= 6, u  # v5 runs on the deprecated Node 20


def test_daily_schedule_and_manual_inputs():
    wf = load("daily-review.yml")
    on = triggers(wf)
    # Started only by workflow_dispatch (external cron-job.org at 09:00 Yerevan + Run button);
    # a schedule: block would add a second daily run.
    assert "schedule" not in on
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["send_telegram"]["default"] is True
    assert inputs["compare_to"]["type"] == "string"


def test_daily_permissions_concurrency_secrets():
    wf = load("daily-review.yml")
    assert wf["permissions"] == {"contents": "write"}
    # Runs queue instead of overlapping: one group for the workflow, never cancelled.
    assert wf["concurrency"] == {"group": "daily-review", "cancel-in-progress": False}
    review = next(s for s in steps(wf) if s.get("id") == "review")
    for secret in ("PSI_API_KEY", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert review["env"][secret] == "${{ secrets.%s }}" % secret
    assert "seo_reviewer.main" in review["run"]
    assert "--compare-to" in review["run"]
    # Scheduled runs always send; only a manual run with the box unticked is a dry run.
    assert "github.event_name == 'schedule'" in review["env"]["NO_DELIVER"]


def test_daily_commit_back_only_after_a_good_review():
    st = steps(load("daily-review.yml"))
    ids = [s.get("id") for s in st]
    assert ids.index("tests") < ids.index("review") < ids.index("commit")
    commit = st[ids.index("commit")]
    assert commit["if"] == "steps.review.outcome == 'success'"
    run = commit["run"]
    assert "git add data/ reports/" in run
    assert 'git commit -m "report: $DAY"' in run
    assert "git diff --cached --quiet" in run  # nothing to commit = no commit
    # Same-day re-runs push the same files: a rebase conflict keeps this run's files
    # (-X theirs = the commit being replayed) and a failed rebase is aborted, not left half done.
    assert "git pull --rebase -X theirs" in run
    assert "git rebase --abort" in run


def test_daily_failure_alert_and_delivery_warning():
    st = steps(load("daily-review.yml"))
    alert = next(s for s in st if s.get("if") == "failure()")
    assert "api.telegram.org" in alert["run"]
    assert "RUN_URL" in alert["env"]
    warn = next(s for s in st if "delivery.json" in s.get("run", ""))
    assert "::warning::" in warn["run"]
    assert warn["if"] == "steps.review.outcome == 'success'"
