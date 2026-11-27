#!/usr/bin/env python3
"""hydra-deploy (PET-102): plan clusters/*/hydra.yaml on pull requests, apply
the reviewed plan after merge.

Run by .github/workflows/hydra-deploy.yaml on a GitHub-hosted runner that
reaches the management cluster's API through the tailnet. Two modes:

plan   (pull_request_target, same-repository PRs into main only)
       Plans every hydra.yaml the PR changes, as it stands at the PR's head,
       with the pinned hydra binary from main. Nothing from the PR is executed:
       its hydra.yaml files are read as data from git objects. Posts one
       comment with the plans and sets the commit status hydra/plan, and only
       then writes plans.json (uploaded as the artifact hydra-plan-<head sha>),
       so no plan is recorded that was not shown.

apply  (push to main)
       Stateless: every run evaluates every cluster as main's tip has it, so
       a run GitHub dropped while it waited, or a merge that started no run,
       loses nothing. A cluster whose plan shows no changes is left alone.
       For a cluster with changes, finds the merged PR behind the last change
       to its hydra.yaml and applies only if all of these hold:
         - the PR's plan artifact came from this workflow on
           pull_request_target, before the PR was merged (a comment, a branch
           workflow or a later re-run cannot stand in for it), and recorded a
           successful hydra/plan (only those are recorded);
         - the deploy machinery (.github/, scripts/ci/, ci/) on main is the
           one that made the plan: a plan is never applied by code it was not
           made with;
         - hydra.yaml on main is byte for byte the one that was planned;
         - planning main again gives the same plan (anything else is drift,
           or another change in between);
         - the plan has no refusal (`!` lines);
         - the cluster is not plan-only (PLAN_ONLY: hydra-mgmt, which is
           applied by hand after an etcd backup; an admission policy also
           keeps CI from writing to it, see ci/hydra-mgmt-access.yaml).
         - the plan was never applied before: each apply first marks the PR's
           head with the status hydra/apply/<cluster>, set by this workflow's
           token, and a marked plan is never applied again automatically. So a
           change rolled back by hand is not re-applied when the live cluster
           matches its starting point again.
       A blocked cluster is reported again on every run until it is resolved.
       It never passes --prune-pools, --allow-remove, --adopt or
       --force-conflicts, and a hydra.yaml removed from the repository never
       deletes a cluster.

A comment on the PR is for reading only; nothing typed into one approves
anything. Merging is the approval.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import yaml

WORKFLOW_FILE = "hydra-deploy.yaml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW_FILE
MAIN = "main"
ARTIFACT_PREFIX = "hydra-plan-"
COMMENT_MARKER = "<!-- hydra-deploy:plan -->"
APPLY_MARKER = "<!-- hydra-deploy:apply -->"
STATUS_CONTEXT = "hydra/plan"
APPLY_CONTEXT = "hydra/apply/"  # + cluster, on the PR's head: this plan was applied once
BOT = "github-actions[bot]"
PATH_RE = re.compile(r"^clusters/([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)/hydra\.yaml$")
# Self-hosted management clusters: applying one changes the cluster this
# workflow acts through, and an etcd backup comes first, which CI cannot take.
PLAN_ONLY = frozenset({"hydra-mgmt"})
WORK_LINE = re.compile(r"^[+~-] ")
PLAN_TIMEOUT = 10 * 60
APPLY_TIMEOUT = 45 * 60
COMMENT_LIMIT = 60000  # GitHub's limit is 65536 characters
FAILED = ("blocked", "error", "failed")
# Files that change what the apply job runs, or with what rights. A push to
# main runs the workflow as merged, so a PR that changes these is reviewed as
# code, not through its plan: its plans are shown but never recorded.
MACHINERY_RE = re.compile(r"^(\.github/|scripts/ci/|ci/)")


class Blocked(Exception):
    """A reason not to apply a cluster."""


# ---------------------------------------------------------------- pure logic


def cluster_of(path):
    """The cluster name for a clusters/<name>/hydra.yaml path, else None."""
    m = PATH_RE.match(path)
    return m.group(1) if m else None


def changed_clusters(paths):
    """Cluster names whose hydra.yaml is among paths, workloads first."""
    names = {n for n in (cluster_of(p) for p in paths) if n}
    return sorted(names, key=lambda n: (n in PLAN_ONLY, n))


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def identity_problem(name, text):
    """Why this hydra.yaml must not be planned for cluster `name`, or None.

    The directory names the cluster: a file in clusters/a/ that describes
    cluster b would plan and apply b under a's review.
    """
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as e:
        return f"is not valid YAML: {e}"
    if len(docs) != 1 or not isinstance(docs[0], dict):
        return "must hold exactly one YAML document, a mapping"
    meta = docs[0].get("metadata")
    if not isinstance(meta, dict):
        return "has no metadata"
    if meta.get("name") != name:
        return f"has metadata.name {meta.get('name')!r}, not its directory's name {name!r}"
    if meta.get("namespace", "default") != "default":
        return f"has metadata.namespace {meta.get('namespace')!r}; clusters here live in 'default'"
    return None


def classify(plan_text, exit_code):
    """error | refused | changes | nochange, from hydra cluster plan's output.

    Any `!` line counts as a refusal, the one-time unprotected-roll note
    included: a person decides those.
    """
    if exit_code != 0:
        return "error"
    lines = plan_text.splitlines()
    if any(line.startswith("!") for line in lines):
        return "refused"
    if any(WORK_LINE.match(line) for line in lines):
        return "changes"
    return "nochange"


def machinery(paths):
    """The paths among a PR's changes that change the deploy machinery."""
    return sorted(p for p in paths if MACHINERY_RE.match(p))


def plan_state(results, withheld=""):
    """The commit status for a set of plan results: (state, description).

    withheld is why the plans are not recorded (so merging applies none of
    them), or "".
    """
    statuses = [r["status"] for r in results.values()]
    if "error" in statuses:
        return "error", "hydra plan failed; see the comment"
    if withheld:
        return "failure", f"merging applies nothing: {withheld}"
    if "invalid" in statuses or "refused" in statuses:
        return "failure", "a person has to resolve a refusal; see the comment"
    changed = [n for n, r in results.items() if r["status"] == "changes"]
    if any(n in PLAN_ONLY for n in changed):
        if any(n not in PLAN_ONLY for n in changed):
            return "success", "planned; merging applies the workload plans, a plan-only cluster is applied by hand"
        return "success", "planned; a plan-only cluster is applied by hand"
    return "success", "planned; merging applies these plans"


def decide(name, main_text, reviewed, now):
    """What apply does for one cluster: (action, reason).

    action is one of apply, nochange, planonly, deleted, blocked, error.
    main_text is hydra.yaml on main (None if removed), reviewed the plan
    recorded on the PR (None if the PR never planned this cluster), now the
    plan of main_text made just now (None if it was not made).
    """
    if main_text is None:
        return "deleted", "hydra.yaml was removed; removing it never deletes a cluster"
    if reviewed is None:
        return "blocked", "the pull request has no recorded plan for this cluster"
    if reviewed.get("yaml_sha256") != sha256(main_text):
        return "blocked", "hydra.yaml on main is not the one that was planned"
    problem = identity_problem(name, main_text)
    if problem:
        return "blocked", f"hydra.yaml {problem}"
    if now is None or now["status"] == "error":
        return "error", "planning main failed"
    if now["status"] == "nochange":
        return "nochange", "no changes: the live cluster matches hydra.yaml"
    if now["plan"] != reviewed.get("plan"):
        return "blocked", ("the plan of main differs from the reviewed plan: the live cluster "
                           "drifted, or something changed it since; plan again in a new pull request")
    if now["status"] == "refused":
        return "blocked", "the plan has a refusal (`!`); a person has to resolve it"
    if name in PLAN_ONLY:
        return "planonly", "plan-only cluster: apply by hand after an etcd backup"
    return "apply", "the reviewed plan"


def candidate_artifacts(artifacts, head_sha, merged_at):
    """Plan artifacts for head_sha made before the merge, newest first."""
    want = ARTIFACT_PREFIX + head_sha
    good = [a for a in artifacts
            if a.get("name") == want and not a.get("expired")
            and a.get("created_at") and merged_at and a["created_at"] <= merged_at]
    return sorted(good, key=lambda a: a["created_at"], reverse=True)


def trusted_run(run, repo):
    """Whether a workflow run is this workflow, from main's file, on a PR."""
    return (run.get("path") == WORKFLOW_PATH
            and run.get("event") == "pull_request_target"
            and (run.get("repository") or {}).get("full_name") == repo)


def already_applied(statuses, name):
    """Whether this workflow ever marked the plan for name as applied."""
    return any(s.get("context") == APPLY_CONTEXT + name and (s.get("creator") or {}).get("login") == BOT
               for s in statuses)


def machinery_matches(plans, digest):
    """Whether plans were made by the deploy machinery whose digest is given."""
    return bool(digest) and plans.get("machinery") == digest


def plans_match(plans, repo, pr):
    return (plans.get("repository") == repo and plans.get("pr") == pr["number"]
            and plans.get("head_sha") == pr["head"]["sha"])


def pick_pr(pulls, commit, tip, is_ancestor):
    """The merged PR behind commit on main: its merge commit lies between
    commit and tip (a squash or merge commit is commit itself; a rebase merge
    ends at or after it). If several do (a branch built on another PR's), the
    first merged brought commit to main."""
    found = [p for p in pulls
             if p.get("merged_at") and (p.get("base") or {}).get("ref") == MAIN
             and p.get("merge_commit_sha")
             and is_ancestor(commit, p["merge_commit_sha"]) and is_ancestor(p["merge_commit_sha"], tip)]
    return min(found, key=lambda p: (p["merged_at"], p.get("number", 0)), default=None)


def evaluate(name, text, now, find_reviewed):
    """(action, reason) for one cluster at main's tip, planning first.

    now is the plan of text made just now, or None when text fails the
    identity check (it is not planned then). find_reviewed() returns the
    reviewed plan of the PR behind the last change, or raises Blocked; it is
    called only when there is something to apply.
    """
    problem = identity_problem(name, text)
    if problem:
        return "blocked", f"hydra.yaml {problem}"
    if now is None or now["status"] == "error":
        return "error", "planning main failed"
    if now["status"] == "nochange":
        return "nochange", "no changes: the live cluster matches hydra.yaml"
    try:
        reviewed = find_reviewed()
    except Blocked as e:
        return "blocked", str(e)
    return decide(name, text, reviewed, now)


def fence(text, lang=""):
    """text in a code block that its own backticks cannot close."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def plan_comment(head_sha, results, withheld=""):
    """The plan comment and whether it shows every plan in full. A plan that
    does not fit is not recorded (see plan_mode): merging applies only what
    the comment showed."""
    body = _plan_comment(head_sha, results, withheld)
    if len(body) <= COMMENT_LIMIT:
        return body, True
    reason = withheld or "the plans are too long to show in full in this comment"
    body = _plan_comment(head_sha, results, reason)
    cut = body[:COMMENT_LIMIT]
    fence_open = open_fence(cut)
    if fence_open:
        cut += "\n" + fence_open
    return cut + "\n\n(truncated; the full plans are in the run's log)", False


def _plan_comment(head_sha, results, withheld):
    label = {"nochange": "No changes", "changes": "Changes", "refused": "**Refused**",
             "error": "**Plan failed**", "invalid": "**Not planned**", "deleted": "Removed"}
    # Headings first, so a truncated comment still says what each plan is.
    out = [COMMENT_MARKER, f"### hydra plan at {head_sha[:12]}", ""]
    for name, r in results.items():
        note = " (plan-only: applied by hand after an etcd backup)" if name in PLAN_ONLY else ""
        out.append(f"- `{name}`: {label[r['status']]}{note}")
    out.append("")
    if withheld:
        out.append(f"**Merging this applies nothing:** {withheld}. Apply by hand after merging, "
                   "or split the change so each pull request's plan can be shown and applied.")
        out.append("")
    out.append("Merging applies a cluster's plan only if, at merge, its hydra.yaml is the one "
               "planned here and planning it again gives this same plan. This comment is for "
               "reading only; nothing written in a comment approves anything.")
    for name, r in results.items():
        out += ["", f"#### `{name}`"]
        if r["status"] in ("invalid", "deleted"):
            out.append(fence(r["message"]))  # may quote the PR's own text
        else:
            out.append(fence(r["plan"] or "(no output)", "diff"))
            if r["status"] == "error" and r.get("stderr"):
                out.append(fence(r["stderr"]))
    return "\n".join(out)


def open_fence(text):
    """The fence of a code block still open at the end of text, or ""."""
    current = ""
    for line in text.split("\n"):
        m = re.match(r"^(`{3,})", line)
        if not m:
            continue
        if not current:
            current = m.group(1)
        elif line.strip() == "`" * len(line.strip()) and len(line.strip()) >= len(current):
            current = ""
    return current


def cell(text):
    """text safe inside a Markdown table cell (a reason can quote a PR's
    hydra.yaml, or a multi-line YAML error)."""
    return " ".join(str(text).split()).replace("\\", "\\\\").replace("|", "\\|")


def apply_comment(sha, outcomes):
    out = [APPLY_MARKER, f"### hydra apply at {sha[:12]}", "", "| Cluster | Result |", "|---|---|"]
    for name, (action, reason) in outcomes.items():
        out.append(f"| `{name}` | **{action}**: {cell(reason)} |")
    return "\n".join(out)


# ------------------------------------------------------------- side effects


def run(args, cwd=None, timeout=None, check=False):
    return subprocess.run(args, cwd=cwd, timeout=timeout, check=check,
                          text=True, capture_output=True)


def git(*args):
    return run(["git", *args], check=True).stdout


def machinery_digest(rev):
    """A digest of the deploy machinery's trees at rev (see MACHINERY_RE)."""
    return sha256(git("ls-tree", rev, "--", ".github", "scripts/ci", "ci"))


def is_ancestor(a, b):
    return run(["git", "merge-base", "--is-ancestor", a, b]).returncode == 0


def blob(rev, path):
    r = run(["git", "show", f"{rev}:{path}"])
    return r.stdout if r.returncode == 0 else None


def gh(*args, data=None):
    """gh api, returning parsed JSON (or None for an empty body)."""
    cmd = ["gh", "api", *args]
    if data is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                       text=True, capture_output=True, check=True)
    return json.loads(r.stdout) if r.stdout.strip() else None


def hydra_plan(name, text, work, hydra, kubeconfig):
    """Plan text for one cluster. The file always sits at <name>/hydra.yaml
    under work, so the plan reads the same in both modes."""
    d = work / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "hydra.yaml").write_text(text)
    try:
        r = run([hydra, "cluster", "plan", "-f", f"{name}/hydra.yaml",
                 "--management-kubeconfig", kubeconfig], cwd=work, timeout=PLAN_TIMEOUT)
        out, err, code = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        out, err, code = "", f"hydra cluster plan did not finish within {PLAN_TIMEOUT}s", 1
    res = {"yaml_sha256": sha256(text), "plan": out, "exit": code, "status": classify(out, code)}
    if code != 0:  # only then: it names the management API and local paths
        res["stderr"] = err[-4000:]
    return res


def hydra_apply(name, work, hydra, kubeconfig):
    try:
        r = subprocess.run([hydra, "cluster", "apply", "-f", f"{name}/hydra.yaml",
                            "--management-kubeconfig", kubeconfig, "--timeout", "40m",
                            "--kubeconfig-out", str(work / f"{name}.kubeconfig")],
                           cwd=work, timeout=APPLY_TIMEOUT, text=True)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        # apply writes the workload cluster's admin kubeconfig; never keep it.
        (work / f"{name}.kubeconfig").unlink(missing_ok=True)


def upsert_comment(repo, number, body, marker=COMMENT_MARKER):
    comments = gh(f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate", "--slurp")
    mine = [c for page in comments for c in page
            if c.get("user", {}).get("login") == "github-actions[bot]"
            and marker in (c.get("body") or "")]
    if mine:
        gh(f"repos/{repo}/issues/comments/{mine[-1]['id']}", "-X", "PATCH", data={"body": body})
    else:
        gh(f"repos/{repo}/issues/{number}/comments", "-X", "POST", data={"body": body})


def set_status(repo, sha, state, desc, context=STATUS_CONTEXT):
    gh(f"repos/{repo}/statuses/{sha}", "-X", "POST",
       data={"state": state, "context": context, "description": desc[:140]})


def plan_mode(a, event, repo):
    pr = event["pull_request"]
    if (pr.get("head", {}).get("repo") or {}).get("full_name") != repo:
        print("::notice::a pull request from a fork is never planned")
        return 0
    if pr["base"]["ref"] != MAIN:
        print(f"::notice::only pull requests into {MAIN} are planned")
        return 0
    number, base, head = pr["number"], pr["base"]["sha"], pr["head"]["sha"]
    try:
        # Objects only: the PR's files are read as data, never checked out or
        # run. The base is in main's history (checked out with full depth).
        git("fetch", "--no-tags", "origin", f"+refs/pull/{number}/head:refs/hydra-deploy/head")
        paths = git("diff", "--name-only", "--no-renames", f"{base}...{head}").split()
        names = changed_clusters(paths)
        if not names:
            print("no hydra.yaml changes")
            return 0
        results = {}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for name in names:
                text = blob(head, f"clusters/{name}/hydra.yaml")
                if text is None:
                    results[name] = {"status": "deleted", "message":
                                     "hydra.yaml is removed; merging this never deletes the cluster."}
                    continue
                problem = identity_problem(name, text)
                if problem:
                    results[name] = {"status": "invalid", "message": f"hydra.yaml {problem}."}
                    continue
                results[name] = hydra_plan(name, text, work, a.hydra, a.kubeconfig)
        mixed = machinery(paths)
        withheld = (f"this pull request also changes the deploy machinery ({', '.join(mixed)}), "
                    "which runs on merge and is reviewed as code, not through a plan") if mixed else ""
        body, complete = plan_comment(head, results, withheld)
        if not complete and not withheld:
            withheld = "the plans are too long to show in full in the comment"
        upsert_comment(repo, number, body)
        state, desc = plan_state(results, "deploy machinery changed" if mixed else
                                 "plan too long to show" if withheld else "")
        set_status(repo, head, state, desc)
    except Exception:
        try:
            set_status(repo, head, "error", "hydra-deploy failed before planning finished; see the run")
        except Exception:  # pylint: disable=broad-except
            pass
        raise
    for name, r in results.items():
        print(f"{name}: {r['status']}")
        if r.get("plan"):
            print(r["plan"])  # the full plan, which a truncated comment does not show
    if withheld:
        print(f"::warning::plans not recorded: {withheld}")
        return 1
    if state != "success":
        # Only a successful plan is ever recorded, so apply never weighs one
        # that a person was asked to look at.
        print(f"::warning::plans not recorded: hydra/plan is {state}")
        return 1
    # Recorded only once shown: apply trusts nothing the PR did not display.
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plans.json").write_text(json.dumps(
        {"repository": repo, "pr": number, "head_sha": head,
         "machinery": machinery_digest("HEAD"), "results": results}, indent=1))
    return 0 if state == "success" else 1


def reviewed_plans(repo, pr, machinery):
    """plans.json recorded for the PR's final head before it merged, by the
    deploy machinery whose digest is machinery, or raise Blocked. Artifacts
    that fail a check are skipped, not fatal: anything can upload an artifact
    by that name, only this workflow's are believed."""
    head = pr["head"]["sha"]
    stale = False
    pages = gh(f"repos/{repo}/actions/artifacts?per_page=100&name={ARTIFACT_PREFIX}{head}",
               "--paginate", "--slurp") or []
    listing = [art for page in pages for art in page.get("artifacts", [])]
    for art in candidate_artifacts(listing, head, pr["merged_at"]):
        run_ = gh(f"repos/{repo}/actions/runs/{art['workflow_run']['id']}")
        if not trusted_run(run_, repo):
            continue
        raw = subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{art['id']}/zip"],
                             capture_output=True, check=True).stdout
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            plans = json.loads(z.read("plans.json"))
        if plans_match(plans, repo, pr):
            if machinery_matches(plans, machinery):
                return plans["results"]
            stale = True
    if stale:
        raise Blocked("the deploy machinery on main changed after this pull request was planned; "
                      "plan it again in a new pull request")
    raise Blocked(f"no plan of this workflow was recorded for the pull request's final head "
                  f"{head[:12]} before it was merged")


def pr_for(repo, commit, tip, fresh):
    """The merged PR behind commit. A fresh merge may not be indexed yet, so
    retry for it; an old commit gets one look."""
    attempts = 6 if fresh else 1
    for attempt in range(attempts):
        pr = pick_pr(gh(f"repos/{repo}/commits/{commit}/pulls") or [], commit, tip, is_ancestor)
        if pr is not None:
            return pr
        if attempt < attempts - 1:
            time.sleep(5)
    return None


def apply_one(a, repo, name, tip, before, work, plans_cache):
    """(action, reason, pr, commit) for one cluster at tip."""
    path = f"clusters/{name}/hydra.yaml"
    text = blob(tip, path)
    commit = git("log", "--first-parent", "-1", "--format=%H", tip, "--", path).strip()
    fresh = bool(commit) and commit != before and (set(before) == {"0"} or is_ancestor(before, commit))
    found = {}

    def find_reviewed():
        pr = pr_for(repo, commit, tip, fresh) if commit else None
        if pr is None:
            raise Blocked(f"no merged pull request behind {commit[:12] or 'this file'} "
                          "(a direct push has no reviewed plan)")
        found["pr"] = pr
        if pr["number"] not in plans_cache:
            try:
                plans_cache[pr["number"]] = reviewed_plans(repo, pr, machinery_digest(tip))
            except Blocked as e:
                plans_cache[pr["number"]] = e
        plans = plans_cache[pr["number"]]
        if isinstance(plans, Blocked):
            raise plans
        return plans.get(name)

    now = hydra_plan(name, text, work, a.hydra, a.kubeconfig) if identity_problem(name, text) is None else None
    action, reason = evaluate(name, text, now, find_reviewed)
    if action == "apply":
        pr = found["pr"]
        head = pr["head"]["sha"]
        statuses = [st for page in gh(f"repos/{repo}/commits/{head}/statuses?per_page=100", "--paginate", "--slurp")
                    for st in page]
        if already_applied(statuses, name):
            action, reason = "blocked", (
                f"#{pr['number']}'s plan for this cluster was applied before, and the live cluster is back "
                "where that plan started (rolled back by hand?). It is never re-applied automatically: "
                "apply by hand, or open a follow-up pull request")
        else:
            ctx = APPLY_CONTEXT + name
            set_status(repo, head, "pending", "applying the reviewed plan", ctx)
            print(f"::group::hydra cluster apply {name}")
            ok = hydra_apply(name, work, a.hydra, a.kubeconfig)
            print("::endgroup::")
            set_status(repo, head, "success" if ok else "failure",
                       "applied" if ok else "apply failed; see the run", ctx)
            action, reason = ("applied", "the reviewed plan") if ok else ("failed", "hydra cluster apply failed; see the run")
    pr = found.get("pr")
    if pr is None and fresh and action == "nochange":
        pr = pr_for(repo, commit, tip, True)  # tell the merged PR it needed nothing
    return action, reason, pr, fresh


def apply_mode(a, event, repo):
    git("fetch", "--no-tags", "origin", f"+refs/heads/{MAIN}:refs/hydra-deploy/{MAIN}")
    tip = git("rev-parse", f"refs/hydra-deploy/{MAIN}").strip()
    before = event["before"]
    names = changed_clusters(git("ls-tree", "-r", "--name-only", tip, "--", "clusters/").split())
    print(f"evaluating {', '.join(names) or 'no clusters'} at {MAIN} {tip[:12]}")
    outcomes, by_pr, cache = {}, {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name in names:
            try:
                action, reason, pr, fresh = apply_one(a, repo, name, tip, before, work, cache)
            except Exception as e:  # report it, and go on with the next cluster
                action, reason, pr, fresh = "error", f"{type(e).__name__}: {e}"[:500], None, False
            outcomes[name] = (action, reason)
            # The merged PR hears about its own merge; an older PR only when
            # something was applied to it now (a dropped run caught up).
            # Other outcomes on old PRs go to the run log, not to their
            # subscribers.
            if pr is not None and (fresh or action in ("applied", "failed")):
                by_pr.setdefault(pr["number"], {})[name] = (action, reason)
            shutil.rmtree(work / name, ignore_errors=True)
    for name, (action, reason) in outcomes.items():
        level = "error" if action in FAILED else ("warning" if action == "planonly" else "notice")
        print(f"::{level}::{name}: {action}: {reason}")
    for number, outs in by_pr.items():
        try:
            gh(f"repos/{repo}/issues/{number}/comments", "-X", "POST", data={"body": apply_comment(tip, outs)})
        except subprocess.CalledProcessError as e:
            print(f"::warning::could not comment on #{number}: {e.stderr}")
    return 1 if any(o[0] in FAILED for o in outcomes.values()) else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("mode", choices=["plan", "apply"])
    p.add_argument("--hydra", required=True)
    p.add_argument("--kubeconfig", required=True)
    p.add_argument("--out", default="hydra-plan", help="plan: where plans.json is written")
    a = p.parse_args(argv)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repo = os.environ["GITHUB_REPOSITORY"]
    return plan_mode(a, event, repo) if a.mode == "plan" else apply_mode(a, event, repo)


if __name__ == "__main__":
    sys.exit(main())
