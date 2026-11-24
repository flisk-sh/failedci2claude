#!/usr/bin/env python3
"""failedci2claude: a Claude Code Stop hook that tells Claude when CI failed on one of your open pull requests.

When a turn ends, it looks at the branches checked out in this repo's worktrees, finds the ones with an open pull
request of yours (one `gh pr list` call), and if any check failed it hands Claude the failing check names so the
session can fix them. It also reports a merge conflict with the base branch. Each (PR, commit, problem) is reported once, so it cannot loop.
"""
import json, os, subprocess, sys, time

# Hooks can start with a short PATH that lacks Homebrew, where gh usually lives.
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"

HOOKS_DIR = os.path.expanduser("~/.claude/hooks")
SEEN = os.path.join(HOOKS_DIR, "failedci2claude.seen.json")
LOG = os.path.join(HOOKS_DIR, "failedci2claude.log")
BAD = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}


def log(msg):
    """One line per run, so a silent hook can be told apart from a hook that never ran."""
    try:
        os.makedirs(HOOKS_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def run(args, cwd, timeout):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if data.get("stop_hook_active"):
        return
    cwd = data.get("cwd") or os.getcwd()
    # A session often opens its PRs from other worktrees, so look at the branch of every worktree of this repo.
    try:
        wt = run(["git", "worktree", "list", "--porcelain"], cwd, 10)
        branches = [l[len("branch refs/heads/"):] for l in wt.stdout.splitlines() if l.startswith("branch refs/heads/")]
        cur = run(["git", "branch", "--show-current"], cwd, 10).stdout.strip()
        if cur and cur not in branches:
            branches.append(cur)
    except Exception as e:
        log(f"git failed: {e}")
        return
    branches = [b for b in dict.fromkeys(branches) if b not in ("main", "master")]
    if not branches:
        return
    # One call for your own open PRs, not one per branch: a repo with many worktrees would exceed the hook timeout.
    try:
        out = run(
            ["gh", "pr", "list", "--state", "open", "--author", "@me", "--limit", "50",
             "--json", "number,url,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,statusCheckRollup"],
            cwd, 25,
        )
        if out.returncode != 0:
            log(f"gh pr list failed: {(out.stderr or '').strip()[:160]}")
            return
        # A single head branch can have open PRs against different bases.
        prs = [(p["headRefName"], p) for p in json.loads(out.stdout)]
    except Exception as e:
        log(f"gh error: {e}")
        return
    try:
        seen = json.load(open(SEEN))
    except Exception:
        seen = []
    reasons = []
    for branch, pr in prs:
        if branch not in branches:
            continue
        failing = sorted({
            c.get("name") or c.get("context") or "?"
            for c in pr.get("statusCheckRollup") or []
            if (c.get("conclusion") or c.get("state") or "").upper() in BAD
        })
        # A merge conflict is not a check, but it keeps a pull request from going green just the same.
        conflict = pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "DIRTY"
        if not failing and not conflict:
            continue
        key = f"{pr['url']}:{pr['headRefOid']}:{','.join(failing)}" + (":conflict" if conflict else "")
        if key in seen:
            continue
        seen.append(key)
        parts = []
        if failing:
            parts.append("failed checks: " + ", ".join(failing))
        if conflict:
            parts.append("merge conflicts with " + (pr.get("baseRefName") or "the base branch"))
        reasons.append(f"{pr['url']} (branch {branch}): " + "; ".join(parts))
    log(f"checked {len(branches)} branches, {len(prs)} open PRs; failing: {reasons or 'none'}")
    if not reasons:
        return
    try:
        os.makedirs(HOOKS_DIR, exist_ok=True)
        json.dump(seen[-200:], open(SEEN, "w"))
    except Exception as e:
        log(f"could not save seen file: {e}")
    print(json.dumps({
        "decision": "block",
        "reason": (
            "Your pull requests need attention: " + "; ".join(reasons) + ". "
            "Work in each branch's worktree. For a failed check, find the cause "
            "(gh run list --branch <branch>; gh run view <id> --log-failed), fix it, verify, commit and push. "
            "For a merge conflict, fetch the base branch, merge it into the PR branch (never rebase or force-push), "
            "resolve keeping the intent of both sides, run the checks for the files you touched, commit and push."
        ),
    }))


main()
