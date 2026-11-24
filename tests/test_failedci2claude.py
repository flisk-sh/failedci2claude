"""
failedci2claude.py, the Stop hook that tells Claude when one of your open PRs has failing CI.

Runs the real script against a real temporary git repo with a stub `gh` that plays the PR list, so nothing touches the
network or the user's ~/.claude (HOME points at a temp dir).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hook" / "failedci2claude.py"
URL = "https://github.com/acme/app/pull/{n}"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def env(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    # A second worktree on its own branch: the session's PRs live in worktrees like this one.
    git(repo, "worktree", "add", "-q", "-b", "feature-a", str(tmp_path / "wt-a"))
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    prs = tmp_path / "prs.json"
    prs.write_text("[]")
    gh = bindir / "gh"
    gh.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'pr list' ] || exit 1\ncat {prs}\n")
    gh.chmod(0o755)

    def run(stdin: dict | None = None):
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(stdin if stdin is not None else {"cwd": str(repo)}),
            capture_output=True, text=True,
            env={"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def set_prs(*items: dict):
        prs.write_text(json.dumps(list(items)))

    return run, set_prs, home


def pr(number: int, branch: str, checks: list[dict], sha: str = "abc", **extra) -> dict:
    return {"number": number, "url": URL.format(n=number), "headRefName": branch, "headRefOid": sha,
            "baseRefName": "main", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": checks, **extra}


FAIL = {"name": "Test", "conclusion": "FAILURE"}
OK = {"name": "Lint", "conclusion": "SUCCESS"}


def test_a_failing_pr_in_another_worktree_is_reported(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [FAIL, OK]))
    out = json.loads(run())
    assert out["decision"] == "block"
    assert "pull/7" in out["reason"] and "Test" in out["reason"] and "Lint" not in out["reason"]


def test_the_same_failure_is_reported_once_and_a_new_commit_again(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [FAIL]))
    assert run()
    assert run() == ""
    set_prs(pr(7, "feature-a", [FAIL], sha="def"))
    assert run()


def test_failures_in_two_repositories_with_the_same_pr_number_and_commit_are_reported(env, tmp_path):
    run, set_prs, _ = env
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(repo), str(other))
    git(other, "switch", "-q", "-c", "feature-a")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    set_prs(pr(7, "feature-a", [FAIL], sha=sha))
    assert URL.format(n=7) in json.loads(run())["reason"]

    other_url = "https://github.com/acme/other/pull/7"
    set_prs(pr(7, "feature-a", [FAIL], sha=sha, url=other_url))
    notice = run({"cwd": str(other)})
    assert notice, "a failure in another repository must not be suppressed"
    assert other_url in json.loads(notice)["reason"]
    assert run({"cwd": str(other)}) == ""


def test_green_and_unrelated_prs_stay_silent(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [OK]), pr(8, "someone-elses-branch", [FAIL]))
    assert run() == ""


def test_main_is_never_reported(env):
    run, set_prs, _ = env
    set_prs(pr(9, "main", [FAIL]))
    assert run() == ""


def test_it_does_not_loop_when_already_continuing_from_a_stop_hook(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [FAIL]))
    assert run({"stop_hook_active": True}) == ""


def test_every_run_leaves_a_log_line(env):
    run, set_prs, home = env
    set_prs(pr(7, "feature-a", [OK]))
    run()
    assert "checked 1 branches" in (home / ".claude" / "hooks" / "failedci2claude.log").read_text()


def test_a_failing_gh_is_logged_not_silent(env, tmp_path):
    run, set_prs, home = env
    (tmp_path / "bin" / "gh").write_text("#!/bin/sh\necho 'auth required' >&2\nexit 4\n")
    assert run() == ""
    log = (home / ".claude" / "hooks" / "failedci2claude.log").read_text()
    assert "gh pr list failed" in log and "auth required" in log


def test_a_merge_conflict_is_reported_even_when_every_check_is_green(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [OK], mergeable="CONFLICTING", mergeStateStatus="DIRTY"))
    out = json.loads(run())
    assert out["decision"] == "block"
    assert "pull/7" in out["reason"] and "merge conflicts with main" in out["reason"]
    assert "never rebase or force-push" in out["reason"]


def test_a_conflict_is_reported_once_per_commit_and_a_new_commit_reports_it_again(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [OK], mergeable="CONFLICTING"))
    assert run()
    assert run() == ""
    set_prs(pr(7, "feature-a", [OK], sha="def", mergeable="CONFLICTING"))
    assert run()


def test_a_failed_check_and_a_conflict_are_one_report(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [FAIL], mergeable="CONFLICTING"))
    reason = json.loads(run())["reason"]
    assert "failed checks: Test" in reason and "merge conflicts with main" in reason


def test_reports_each_failing_pr_when_one_head_targets_multiple_bases(env):
    run, set_prs, _ = env
    set_prs(
        pr(7, "feature-a", [FAIL]),
        {**pr(8, "feature-a", [FAIL]), "baseRefName": "release"},
        {**pr(9, "feature-a", [OK]), "baseRefName": "next"},
    )
    notice = run()
    assert notice, "a green PR must not hide failures for the same branch"
    reason = json.loads(notice)["reason"]
    assert URL.format(n=7) in reason
    assert URL.format(n=8) in reason
    assert URL.format(n=9) not in reason
    assert run() == ""


def test_an_unknown_merge_state_is_not_a_conflict(env):
    run, set_prs, _ = env
    set_prs(pr(7, "feature-a", [OK], mergeable="UNKNOWN", mergeStateStatus="UNKNOWN"))
    assert run() == ""
