#!/usr/bin/env python3
"""hydra-deployer: plan hydra.yaml changes on PRs, and apply them after merge.

A pull-based agent (PET-102, ADR-002 decision 6 as amended). It runs in the lab
as a CronJob, so no repository code ever runs there: from GitHub it reads only
clusters/<name>/hydra.yaml files, and the commands it runs are the pinned
`hydra` binary's own `plan` and `apply`, exactly as a person runs them.

Trust: nothing on GitHub that a person can type is taken as approval. The PR
comment is for reading only. What gets applied is checked against the agent's
own record of the plan it made (in its state ConfigMap), against the merged
content byte for byte, and against an approver: the PR was merged by one, or
one approved it at that head.

Each run:

1. Plan. For every open pull request into main from this repository (never a
   fork) that changes clusters/<name>/hydra.yaml, run `hydra cluster plan` for
   each changed cluster, post the result as one comment, set a commit status
   `hydra/plan`, and record per cluster the PR's head, the sha256 of the
   hydra.yaml planned and the plan's hash. A PR is planned again when its head
   moves, or ten minutes after a plan that failed.

2. Apply. Pin main's head once. For every cluster directory there, when the
   newest commit touching its hydra.yaml is not the one last applied:
   - Its metadata.name must be the directory's name and its namespace default,
     so ordering, backups and state always follow the cluster hydra targets.
   - Plan it again. "No changes": record it as applied.
   - Otherwise apply only if the commit came from a PR merged into main that
     this agent planned at the PR's final head, the hydra.yaml is the one it
     planned, the plan is the same now (otherwise something drifted), there is
     no refusal, and an approver merged or approved it. A management cluster
     goes after every workload cluster, never in a run where a workload apply
     failed, and only after an etcd backup Job succeeds.
   - A commit that cannot be applied is blocked once and reported; a failed
     apply is retried on later runs with backoff.

State lives in a ConfigMap in this agent's namespace, saved after every
cluster: Kubernetes, never the agent's disk (ADR-001).
"""

import base64
import dataclasses
import datetime
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MARKER = "<!-- hydra-plan -->"
APPLY_MARKER = "<!-- hydra-apply -->"
HYDRA_YAML = re.compile(r"^clusters/([a-z0-9]([-a-z0-9]*[a-z0-9])?)/hydra\.yaml$")
NO_CHANGES = "No changes: the live cluster matches hydra.yaml."
COMMENT_LIMIT = 60000  # GitHub allows 65536 characters per comment
MAX_BACKOFF = 3600
REPLAN_AFTER_ERROR = 600
PLAN_RECORD_TTL = 14 * 24 * 3600
PLAN_TIMEOUT = 300
APPLY_TIMEOUT = 1500
APPLY_HYDRA_TIMEOUT = "25m"


def log(message):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


# --- hydra.yaml ------------------------------------------------------------------


def identity(content):
    """(metadata.name, metadata.namespace or None) from a hydra.yaml, read
    conservatively: the top-level `metadata:` block in block style, plain or
    quoted scalars. Anything else is (None, None) and is refused; the
    validator enforces the same shape."""
    name = namespace = None
    in_meta = False
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_meta = line.rstrip() == "metadata:"
            continue
        if in_meta:
            match = re.fullmatch(r"  (name|namespace):\s*([\"']?)([a-z0-9]([-a-z0-9]*[a-z0-9])?)\2\s*(#.*)?", line)
            if match:
                if match.group(1) == "name":
                    name = match.group(3)
                else:
                    namespace = match.group(3)
    return name, namespace


def identity_problem(cluster, content):
    name, namespace = identity(content)
    if name != cluster:
        return f"its metadata.name is {name!r}, not the directory's name {cluster!r}"
    if namespace not in (None, "default"):
        return f"its metadata.namespace is {namespace!r}; the agent only deploys to default"
    return None


# --- plan output ------------------------------------------------------------


def normalize(output):
    """The plan without the line naming the kubeconfig, which varies by host."""
    lines = [line for line in output.splitlines() if not line.startswith("management cluster: ")]
    return "\n".join(lines).strip() + "\n"


def plan_hash(output):
    return hashlib.sha256(normalize(output).encode()).hexdigest()[:16]


def blocking_lines(output):
    """Lines apply would refuse on, or that need a person: refusals and
    conflicts, which hydra prints starting with "! "."""
    return [line for line in output.splitlines() if line.startswith("! ")]


@dataclasses.dataclass
class Plan:
    cluster: str
    code: int
    output: str

    @property
    def ok(self):
        return self.code == 0 and not blocking_lines(self.output)

    @property
    def no_changes(self):
        return self.code == 0 and normalize(self.output).strip() == NO_CHANGES

    @property
    def digest(self):
        return plan_hash(self.output)


def fence(text):
    """A code block nothing in it can close or hide a comment inside."""
    return "```text\n" + text.replace("```", "'''").replace("<!--", "<!- -") + "\n```"


def plan_comment(head, plans, removed, hydra_version):
    parts = [MARKER, f"### hydra plan for `{head[:12]}`", ""]
    for plan in plans:
        if plan.code != 0:
            verdict = "**plan failed**"
        elif not plan.ok:
            verdict = "**blocked**: apply would refuse, see the `!` lines"
        elif plan.no_changes:
            verdict = "no changes"
        else:
            verdict = "applies on merge"
        parts += [f"**`clusters/{plan.cluster}/hydra.yaml`**: {verdict}", "",
                  "<details><summary>plan</summary>", "", fence(normalize(plan.output).strip()), "", "</details>", ""]
    for cluster in removed:
        parts += [f"**`clusters/{cluster}/hydra.yaml`** is removed or renamed: the agent never deletes a "
                  "cluster. Delete it by hand with `hydra cluster delete`.", ""]
    parts.append(f"_Planned by hydra-deployer with hydra {hydra_version}. On merge each cluster is applied only "
                 "if an approver merged or approved this head, its hydra.yaml is the one planned here, and its "
                 "plan is still exactly this one._")
    body = "\n".join(parts)
    if len(body) > COMMENT_LIMIT:
        body = body[: COMMENT_LIMIT - 40] + "\n\n_(plan truncated)_\n"
    return body


# --- GitHub -----------------------------------------------------------------


class GitHub:
    def __init__(self, repo, token, api="https://api.github.com", opener=urllib.request.urlopen):
        self.repo, self.token, self.api, self.opener = repo, token, api.rstrip("/"), opener

    def request(self, method, path, body=None, raw=False):
        url = path if path.startswith("http") else f"{self.api}/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github.raw" if raw else "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "hydra-deployer")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self.opener(req, timeout=60) as resp:
            payload = resp.read()
        if raw:
            return payload.decode()
        return json.loads(payload) if payload else None

    def pages(self, path):
        sep = "&" if "?" in path else "?"
        page = 1
        while True:
            items = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            yield from items
            if len(items) < 100:
                return
            page += 1

    def open_pulls(self):
        return list(self.pages("/pulls?state=open"))

    def pull_files(self, number):
        return list(self.pages(f"/pulls/{number}/files"))

    def file_at(self, path, ref):
        try:
            return self.request("GET", f"/contents/{urllib.parse.quote(path)}?ref={ref}", raw=True)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def put_comment(self, number, body, comment_id):
        """Update the agent's own comment, or post one; returns its id."""
        if comment_id:
            try:
                self.request("PATCH", f"/issues/comments/{comment_id}", {"body": body})
                return comment_id
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        return self.request("POST", f"/issues/{number}/comments", {"body": body})["id"]

    def comment(self, number, body):
        self.request("POST", f"/issues/{number}/comments", {"body": body})

    def status(self, sha, context, state, description):
        self.request("POST", f"/statuses/{sha}", {"context": context, "state": state, "description": description[:140]})

    def head(self, branch):
        return self.request("GET", f"/commits/{urllib.parse.quote(branch)}")["sha"]

    def cluster_dirs(self, ref):
        try:
            entries = self.request("GET", f"/contents/clusters?ref={ref}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return []
            raise
        names = []
        for entry in entries:
            if entry.get("type") == "dir" and HYDRA_YAML.match(f"clusters/{entry['name']}/hydra.yaml"):
                if self.file_at(f"clusters/{entry['name']}/hydra.yaml", ref) is not None:
                    names.append(entry["name"])
        return sorted(names)

    def latest_commit(self, path, ref):
        commits = self.request("GET", f"/commits?sha={ref}&path={urllib.parse.quote(path)}&per_page=1")
        return commits[0]["sha"] if commits else None

    def merged_pull(self, sha, branch):
        """The PR merged into branch that this commit came from, if any."""
        for pull in self.request("GET", f"/commits/{sha}/pulls"):
            if pull.get("merged_at") and pull.get("base", {}).get("ref") == branch:
                return self.request("GET", f"/pulls/{pull['number']}")  # for merged_by
        return None

    def approved(self, pull, approvers):
        """An approver merged it, or approved its final head."""
        if (pull.get("merged_by") or {}).get("login") in approvers:
            return True
        for review in self.pages(f"/pulls/{pull['number']}/reviews"):
            if (review.get("state") == "APPROVED" and review.get("commit_id") == pull["head"]["sha"]
                    and (review.get("user") or {}).get("login") in approvers):
                return True
        return False


# --- Kubernetes (the cluster the agent runs on) -------------------------------


class Kube:
    """Just enough of the API for the state ConfigMap and the backup Job, with
    the pod's own ServiceAccount."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, host=None, token=None, cafile=None, opener=None):
        host = host or f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
        self.host = host
        if token is None:
            with open(f"{self.SA}/token") as f:
                token = f.read().strip()
        self.token = token
        context = ssl.create_default_context(cafile=cafile or f"{self.SA}/ca.crt")
        self.opener = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout, context=context))

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.host + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self.opener(req, timeout=60) as resp:
            payload = resp.read()
        return json.loads(payload) if payload else None

    def get_state(self, namespace, name):
        try:
            return self.request("GET", f"/api/v1/namespaces/{namespace}/configmaps/{name}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        return self.request("POST", f"/api/v1/namespaces/{namespace}/configmaps",
                            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, "data": {}})

    def put_state(self, cm):
        """Replace the ConfigMap, guarded by its resourceVersion; a conflict is
        raised as HTTPError 409."""
        md = cm["metadata"]
        return self.request("PUT", f"/api/v1/namespaces/{md['namespace']}/configmaps/{md['name']}", cm)

    def run_backup(self, namespace, cronjob, timeout=900, poll=10):
        """Create a Job from the CronJob's template, as `kubectl create job
        --from=cronjob/...` does, and wait for it."""
        cj = self.request("GET", f"/apis/batch/v1/namespaces/{namespace}/cronjobs/{cronjob}")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        job = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": f"{cronjob[:40]}-deployer-{stamp}", "namespace": namespace,
                         "annotations": {"cronjob.kubernetes.io/instantiate": "manual"},
                         "labels": {"app.kubernetes.io/created-by": "hydra-deployer"}},
            "spec": cj["spec"]["jobTemplate"]["spec"],
        }
        created = self.request("POST", f"/apis/batch/v1/namespaces/{namespace}/jobs", job)
        name = created["metadata"]["name"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.request("GET", f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}").get("status", {})
            for cond in status.get("conditions", []):
                if cond.get("status") == "True" and cond.get("type") == "Complete":
                    return True, name
                if cond.get("status") == "True" and cond.get("type") == "Failed":
                    return False, name
            time.sleep(poll)
        return False, name


class State:
    """The state ConfigMap's data, saved after every change that matters.

    Only the keys this run changed are written back: on a conflict (someone
    edited the ConfigMap, for example to unblock a cluster) the fresh copy is
    read and the same changes applied to it."""

    def __init__(self, kube, namespace, name):
        self.kube, self.namespace, self.name = kube, namespace, name
        self.cm = kube.get_state(namespace, name)
        self.cm["data"] = self.cm.get("data") or {}
        self.changes = {}

    @property
    def data(self):
        return self.cm["data"]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.changes[key] = value

    def drop(self, key):
        if key in self.data:
            del self.data[key]
            self.changes[key] = None

    def save(self):
        if not self.changes:
            return
        for attempt in range(3):
            try:
                self.cm = self.kube.put_state(self.cm) or self.cm
                self.cm["data"] = self.cm.get("data") or {}
                self.changes = {}
                return
            except urllib.error.HTTPError as error:
                if error.code != 409 or attempt == 2:
                    raise
                fresh = self.kube.get_state(self.namespace, self.name)
                fresh["data"] = fresh.get("data") or {}
                for key, value in self.changes.items():
                    if value is None:
                        fresh["data"].pop(key, None)
                    else:
                        fresh["data"][key] = value
                self.cm = fresh


# --- hydra --------------------------------------------------------------------


class Hydra:
    def __init__(self, binary, kubeconfig, workdir, version):
        self.binary, self.kubeconfig, self.workdir, self.version = binary, kubeconfig, workdir, version

    def _file(self, cluster, content):
        path = os.path.join(self.workdir, f"{cluster}.hydra.yaml")
        with open(path, "w") as f:
            f.write(content)
        return path

    def _run(self, args, timeout):
        try:
            result = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"hydra {' '.join(args[:2])} timed out after {timeout}s"
        return result.returncode, (result.stdout + result.stderr)

    def plan(self, cluster, content):
        code, output = self._run(["cluster", "plan", "-f", self._file(cluster, content),
                                  "--management-kubeconfig", self.kubeconfig], timeout=PLAN_TIMEOUT)
        return Plan(cluster, code, output)

    def apply(self, cluster, content):
        out = os.path.join(self.workdir, f"{cluster}.kubeconfig")
        return self._run(["cluster", "apply", "-f", self._file(cluster, content),
                          "--management-kubeconfig", self.kubeconfig, "--kubeconfig-out", out,
                          "--timeout", APPLY_HYDRA_TIMEOUT], timeout=APPLY_TIMEOUT)


# --- the agent ------------------------------------------------------------------


@dataclasses.dataclass
class Config:
    repo: str
    branch: str
    state_namespace: str
    state_name: str
    management_clusters: frozenset
    backups: dict  # cluster -> (namespace, cronjob)
    approvers: frozenset


def refused(cluster, message):
    """A plan the agent made without running hydra: the file is not one it
    will deploy."""
    return Plan(cluster, 2, f"hydra-deployer: not planned: {message}")


class Deployer:
    def __init__(self, config, github, kube, hydra_factory, now=time.time):
        self.config, self.github, self.kube, self.hydra_factory, self.now = config, github, kube, hydra_factory, now
        self._hydra = None

    @property
    def hydra(self):
        # Fetched only when a run has work: idle runs cost a few API calls.
        if self._hydra is None:
            self._hydra = self.hydra_factory()
        return self._hydra

    def run(self):
        state = State(self.kube, self.config.state_namespace, self.config.state_name)
        try:
            self.plan_pulls(state)
        except Exception as error:  # planning never holds back applies
            log(f"planning failed: {type(error).__name__}: {error}")
        self.apply_main(state)

    # 1. plan ---------------------------------------------------------------------

    def plan_pulls(self, state):
        open_numbers = set()
        for pull in self.github.open_pulls():
            open_numbers.add(str(pull["number"]))
            try:
                self.plan_pull(pull, state)
            except Exception as error:
                log(f"PR #{pull['number']}: planning failed: {type(error).__name__}: {error}")
            state.save()
        self.forget_old_plans(state, open_numbers)
        state.save()

    def plan_pull(self, pull, state):
        head = pull["head"]
        if (head.get("repo") or {}).get("full_name") != self.config.repo:
            return  # forks never reach the management cluster
        if pull["base"]["ref"] != self.config.branch:
            return
        changed, removed = set(), set()
        for f in self.github.pull_files(pull["number"]):
            match = HYDRA_YAML.match(f["filename"])
            if match:
                (removed if f["status"] == "removed" else changed).add(match.group(1))
            old = HYDRA_YAML.match(f.get("previous_filename") or "")
            if old and f["status"] == "renamed":
                removed.add(old.group(1))
        if not changed and not removed:
            return
        prefix = f"plan.{pull['number']}"
        if state.get(f"{prefix}.head") == head["sha"]:
            failed = state.get(f"{prefix}.result") == "error"
            if not failed or self.now() - float(state.get(f"{prefix}.at", "0")) < REPLAN_AFTER_ERROR:
                return  # already planned for this head
        log(f"planning PR #{pull['number']} at {head['sha'][:12]}: {', '.join(sorted(changed | removed))}")
        plans = []
        for cluster in sorted(changed):
            content = self.github.file_at(f"clusters/{cluster}/hydra.yaml", head["sha"])
            if content is None:
                plans.append(refused(cluster, "the file could not be read at the PR's head"))
                continue
            problem = identity_problem(cluster, content)
            plan = refused(cluster, problem) if problem else self.hydra.plan(cluster, content)
            plans.append(plan)
            key = f"{prefix}.{cluster}"
            if plan.ok:
                state.set(key, json.dumps({"head": head["sha"], "content": sha256(content), "digest": plan.digest}))
            else:
                state.drop(key)
        for key in [k for k in state.data if k.startswith(prefix + ".")]:
            cluster = key[len(prefix) + 1:]
            if cluster not in {"head", "at", "result", "comment"} and cluster not in changed:
                state.drop(key)  # no longer in the PR
        if any(p.code != 0 for p in plans):
            result, status, desc = "error", "error", "hydra plan failed or the file was refused; see the PR comment"
        elif any(not p.ok for p in plans):
            result, status, desc = "refused", "failure", "apply would refuse; see the ! lines in the PR comment"
        else:
            result, status, desc = "ok", "success", "plan posted; applies on merge by an approver if unchanged"
        version = self.hydra.version if any(p.code != 2 for p in plans) else "(not run)"
        body = plan_comment(head["sha"], plans, sorted(removed), version)
        comment_id = self.github.put_comment(pull["number"], body, state.get(f"{prefix}.comment"))
        state.set(f"{prefix}.comment", str(comment_id))
        state.set(f"{prefix}.head", head["sha"])
        state.set(f"{prefix}.at", str(self.now()))
        state.set(f"{prefix}.result", result)
        self.github.status(head["sha"], "hydra/plan", status, desc)

    def forget_old_plans(self, state, open_numbers):
        """Plans of closed PRs are kept two weeks, long enough to apply a merge."""
        for key in [k for k in state.data if k.startswith("plan.") and k.endswith(".at")]:
            number = key.split(".")[1]
            if number in open_numbers or self.now() - float(state.get(key, "0")) < PLAN_RECORD_TTL:
                continue
            for k in [k for k in state.data if k.startswith(f"plan.{number}.")]:
                state.drop(k)

    # 2. apply ---------------------------------------------------------------------

    def apply_main(self, state):
        main = self.github.head(self.config.branch)  # one main for the whole run
        clusters = self.github.cluster_dirs(main)
        # Workload clusters before management clusters (ADR-002, PET-102).
        ordered = sorted(clusters, key=lambda c: (c in self.config.management_clusters, c))
        workload_failed = False
        for cluster in ordered:
            if cluster in self.config.management_clusters and workload_failed:
                log(f"{cluster}: not applied this run, because a workload cluster failed")
                continue
            try:
                ok = self.apply_cluster(cluster, main, state)
            except Exception as error:
                log(f"{cluster}: failed: {type(error).__name__}: {error}")
                ok = False
            state.save()
            if not ok and cluster not in self.config.management_clusters:
                workload_failed = True

    def apply_cluster(self, cluster, main, state):
        """Returns whether the cluster is applied (or has nothing to apply)."""
        path = f"clusters/{cluster}/hydra.yaml"
        sha = self.github.latest_commit(path, main)
        if not sha or state.get(f"{cluster}.applied") == sha or state.get(f"{cluster}.blocked") == sha:
            return True
        if state.get(f"{cluster}.pending") == sha and self.now() < float(state.get(f"{cluster}.retry-after", "0")):
            return False  # failed before; waiting out the backoff
        content = self.github.file_at(path, sha)
        problem = identity_problem(cluster, content or "")
        pull = self.github.merged_pull(sha, self.config.branch)
        if problem:
            self._block(cluster, sha, pull, problem, None, state)
            return False
        plan = self.hydra.plan(cluster, content)
        if plan.no_changes:
            log(f"{cluster}: {sha[:12]} needs no changes; recorded as applied")
            self._applied(cluster, sha, state)
            return True
        if plan.code != 0:
            return self._failed(cluster, sha, pull, "hydra plan failed", plan.output, state)
        reason = self.unapproved(cluster, content, plan, pull, state)
        if reason:
            self._block(cluster, sha, pull, reason, plan, state)
            return False

        backup = self.config.backups.get(cluster)
        if backup:
            ok, job = self.kube.run_backup(*backup)
            if not ok:
                return self._failed(cluster, sha, pull, f"the etcd backup Job {backup[0]}/{job} did not succeed",
                                    "", state)
            log(f"{cluster}: etcd backup {backup[0]}/{job} succeeded")
        log(f"{cluster}: applying {sha[:12]} (PR #{pull['number']})")
        code, output = self.hydra.apply(cluster, content)
        if code != 0:
            return self._failed(cluster, sha, pull, "hydra cluster apply failed", output, state)
        self._applied(cluster, sha, state)
        state.save()  # recorded before anything else can fail
        self.github.status(sha, f"hydra/apply/{cluster}", "success", "applied")
        self.github.comment(pull["number"], f"{APPLY_MARKER}\n**hydra-deployer applied `clusters/{cluster}/hydra.yaml` "
                                            f"at `{sha[:12]}`.**\n\n<details><summary>apply output</summary>\n\n"
                                            f"{fence(normalize(output).strip()[-20000:])}\n\n</details>")
        return True

    def unapproved(self, cluster, content, plan, pull, state):
        """Why this merge may not be applied, or None."""
        if not plan.ok:
            return "the plan has refusals or conflicts (`!` lines)"
        if pull is None:
            return "it did not come from a pull request merged into main, so no reviewed plan exists"
        record = state.get(f"plan.{pull['number']}.{cluster}")
        if record is None:
            return f"hydra-deployer has no successful plan of this cluster for PR #{pull['number']}"
        record = json.loads(record)
        if record["head"] != pull["head"]["sha"]:
            return f"PR #{pull['number']} was merged at a head hydra-deployer had not planned"
        if record["content"] != sha256(content):
            return "the merged hydra.yaml is not the one planned on the PR"
        if record["digest"] != plan.digest:
            return f"the plan changed since PR #{pull['number']} was reviewed (something drifted)"
        if not self.github.approved(pull, self.config.approvers):
            return f"PR #{pull['number']} was neither merged nor approved at its final head by an approver"
        return None

    def _applied(self, cluster, sha, state):
        state.set(f"{cluster}.applied", sha)
        for key in ("blocked", "pending", "failures", "retry-after"):
            state.drop(f"{cluster}.{key}")

    def _block(self, cluster, sha, pull, reason, plan, state):
        log(f"{cluster}: {sha[:12]} blocked: {reason}")
        state.set(f"{cluster}.blocked", sha)
        for key in ("pending", "failures", "retry-after"):
            state.drop(f"{cluster}.{key}")
        state.save()
        self.github.status(sha, f"hydra/apply/{cluster}", "failure", f"not applied: {reason}")
        if pull:
            shown = f"\n\n<details><summary>plan now</summary>\n\n{fence(normalize(plan.output).strip()[-20000:])}\n\n</details>" if plan else ""
            self.github.comment(pull["number"], f"**hydra-deployer did not apply `clusters/{cluster}/hydra.yaml` at "
                                                f"`{sha[:12]}`**: {reason}. Apply it by hand after checking "
                                                f"`hydra cluster plan`, or merge a follow-up change.{shown}")

    def _failed(self, cluster, sha, pull, what, output, state):
        failures = int(state.get(f"{cluster}.failures", "0")) + 1 if state.get(f"{cluster}.pending") == sha else 1
        backoff = min(MAX_BACKOFF, 120 * 2 ** (failures - 1))
        state.set(f"{cluster}.pending", sha)
        state.set(f"{cluster}.failures", str(failures))
        state.set(f"{cluster}.retry-after", str(self.now() + backoff))
        state.save()
        log(f"{cluster}: {sha[:12]} {what} (attempt {failures}); retrying in {backoff}s")
        if failures == 1:
            self.github.status(sha, f"hydra/apply/{cluster}", "error", f"{what}; retrying with backoff")
            if pull:
                self.github.comment(pull["number"], f"**hydra-deployer: {what}** for `clusters/{cluster}/hydra.yaml` at "
                                                    f"`{sha[:12]}`. It retries with backoff; rerunning is safe (stateless "
                                                    f"apply).\n\n<details><summary>output</summary>\n\n"
                                                    f"{fence(normalize(output).strip()[-20000:])}\n\n</details>")
        return False


# --- wiring ----------------------------------------------------------------------


def fetch_hydra(version, sha256_hex, workdir, opener=urllib.request.urlopen):
    url = f"https://github.com/Petatron/hydra/releases/download/{version}/hydra-linux-amd64"
    path = os.path.join(workdir, "hydra")
    with opener(url, timeout=300) as resp:
        payload = resp.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != sha256_hex:
        raise RuntimeError(f"hydra {version}: sha256 {digest} is not the pinned {sha256_hex}")
    with open(path, "wb") as f:
        f.write(payload)
    os.chmod(path, 0o755)
    return path


def mgmt_kubeconfig(api_dir, server, workdir):
    """A kubeconfig for the management cluster from the mounted token and CA.
    Written to the pod's memory-backed work directory, never printed."""
    with open(os.path.join(api_dir, "token")) as f:
        token = f.read().strip()
    with open(os.path.join(api_dir, "ca.crt"), "rb") as f:
        ca = base64.b64encode(f.read()).decode()
    path = os.path.join(workdir, "management.kubeconfig")
    config = {
        "apiVersion": "v1", "kind": "Config", "current-context": "mgmt",
        "clusters": [{"name": "mgmt", "cluster": {"server": server, "certificate-authority-data": ca}}],
        "users": [{"name": "deployer", "user": {"token": token}}],
        "contexts": [{"name": "mgmt", "context": {"cluster": "mgmt", "user": "deployer"}}],
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def parse_backups(spec):
    out = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        cluster, _, ref = item.partition("=")
        namespace, _, cronjob = ref.partition("/")
        if not (cluster and namespace and cronjob):
            raise ValueError(f"BACKUP_CRONJOBS: {item!r} is not cluster=namespace/cronjob")
        out[cluster] = (namespace, cronjob)
    return out


def split(value):
    return frozenset(filter(None, (s.strip() for s in value.split(","))))


def main():
    env = os.environ
    workdir = env.get("WORKDIR") or tempfile.mkdtemp(prefix="hydra-deployer-")
    config = Config(
        repo=env["GITHUB_REPOSITORY"],
        branch=env.get("BRANCH", "main"),
        state_namespace=env["STATE_NAMESPACE"],
        state_name=env.get("STATE_CONFIGMAP", "hydra-deployer-state"),
        management_clusters=split(env.get("MANAGEMENT_CLUSTERS", "")),
        backups=parse_backups(env.get("BACKUP_CRONJOBS", "")),
        approvers=split(env["APPROVERS"]),
    )
    if not config.approvers:
        raise ValueError("APPROVERS must name at least one GitHub login")
    with open(env["GITHUB_TOKEN_FILE"]) as f:
        token = f.read().strip()

    def hydra_factory():
        binary = fetch_hydra(env["HYDRA_VERSION"], env["HYDRA_SHA256"], workdir)
        kubeconfig = mgmt_kubeconfig(env["MGMT_API_DIR"], env["MGMT_SERVER"], workdir)
        return Hydra(binary, kubeconfig, workdir, env["HYDRA_VERSION"])

    Deployer(config, GitHub(config.repo, token), Kube(), hydra_factory).run()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # one line, never a traceback that could echo a request
        log(f"run failed: {type(error).__name__}: {error}")
        sys.exit(1)
