#!/usr/bin/env python3
"""hydra-deployer: plan hydra.yaml changes on PRs, and apply them after merge.

A pull-based agent (PET-102, ADR-002 decision 6 as amended). It runs in the lab
as a CronJob, so no repository code ever runs there: from GitHub it reads only
clusters/<name>/hydra.yaml files, and the commands it runs are the pinned
`hydra` binary's own `plan` and `apply`, exactly as a person runs them.

Each run:

1. Plan. For every open pull request from this repository (never a fork)
   whose head changes clusters/*/hydra.yaml, run `hydra cluster plan` for each
   changed cluster and post the result as one comment per PR, plus a commit
   status `hydra/plan`. A plan with a refusal or a conflict ("! " lines), or
   one that fails, sets the status to failure. A PR is planned again only when
   its head moves; the comment records the head it was made for.

2. Apply. For every cluster directory on main, when the newest commit touching
   its hydra.yaml is not the one last applied: plan main again.
   - "No changes": record it as applied; there is nothing to do.
   - Otherwise apply only if that commit came from a merged PR whose plan for
     this cluster is the same as the plan now (the merge is the approval; a
     plan that moved since means something drifted), and the plan has no
     refusal. A management cluster is applied after every workload cluster,
     and only after an etcd backup Job succeeds.
   - A commit that cannot be applied is blocked once and reported; a failed
     apply is retried on later runs with backoff.

State (which commit each cluster was last applied at, what is blocked, retry
backoff) lives in a ConfigMap in this agent's namespace: Kubernetes, never the
agent's disk (ADR-001).
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
META = re.compile(r"<!-- hydra-plan-meta (\{.*?\}) -->", re.S)
HYDRA_YAML = re.compile(r"^clusters/([a-z0-9]([-a-z0-9]*[a-z0-9])?)/hydra\.yaml$")
NO_CHANGES = "No changes: the live cluster matches hydra.yaml."
COMMENT_LIMIT = 60000  # GitHub allows 65536 characters per comment
MAX_BACKOFF = 3600


def log(message):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


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
    """A code block GitHub cannot be talked out of."""
    return "```text\n" + text.replace("```", "'''") + "\n```"


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
        parts += [f"**`clusters/{cluster}/hydra.yaml`** is removed: the agent never deletes a cluster. "
                  "Delete it by hand with `hydra cluster delete`.", ""]
    parts.append(f"_Planned by hydra-deployer with hydra {hydra_version}. On merge, each cluster is applied "
                 "only if its plan is still exactly this one._")
    meta = {"head": head, "plans": {plan.cluster: plan.digest for plan in plans if plan.ok}}
    parts.append(f"<!-- hydra-plan-meta {json.dumps(meta, sort_keys=True)} -->")
    body = "\n".join(parts)
    if len(body) > COMMENT_LIMIT:
        # Keep the meta: it is what apply checks against.
        tail = parts[-2] + "\n" + parts[-1]
        body = body[: COMMENT_LIMIT - len(tail) - 40] + "\n\n_(plan truncated)_\n" + tail
    return body


def comment_meta(body):
    match = META.search(body or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


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

    def plan_comment(self, number):
        for comment in self.pages(f"/issues/{number}/comments"):
            body = comment.get("body") or ""
            if body.startswith(MARKER) and comment_meta(body) is not None:
                return comment
        return None

    def put_plan_comment(self, number, body, existing):
        if existing:
            self.request("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
        else:
            self.request("POST", f"/issues/{number}/comments", {"body": body})

    def comment(self, number, body):
        self.request("POST", f"/issues/{number}/comments", {"body": body})

    def status(self, sha, context, state, description):
        self.request("POST", f"/statuses/{sha}", {"context": context, "state": state, "description": description[:140]})

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

    def latest_commit(self, path, branch):
        commits = self.request("GET", f"/commits?sha={branch}&path={urllib.parse.quote(path)}&per_page=1")
        return commits[0]["sha"] if commits else None

    def merged_pull(self, sha):
        for pull in self.request("GET", f"/commits/{sha}/pulls"):
            if pull.get("merged_at"):
                return pull
        return None


# --- Kubernetes (the cluster the agent runs on) -------------------------------


class Kube:
    """Just enough of the API for the state ConfigMap and the backup Job, with
    the pod's own ServiceAccount."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, host=None, token=None, cafile=None, opener=None):
        host = host or f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
        self.host = host
        self.token = token if token is not None else open(f"{self.SA}/token").read().strip()
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
            cm = self.request("GET", f"/api/v1/namespaces/{namespace}/configmaps/{name}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            cm = self.request("POST", f"/api/v1/namespaces/{namespace}/configmaps",
                              {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, "data": {}})
        return cm

    def put_state(self, cm):
        # resourceVersion guards against a second run: one wins, the other fails.
        return self.request("PUT", f"/api/v1/namespaces/{cm['metadata']['namespace']}/configmaps/{cm['metadata']['name']}", cm)

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
                                  "--management-kubeconfig", self.kubeconfig], timeout=600)
        return Plan(cluster, code, output)

    def apply(self, cluster, content):
        out = os.path.join(self.workdir, f"{cluster}.kubeconfig")
        return self._run(["cluster", "apply", "-f", self._file(cluster, content),
                          "--management-kubeconfig", self.kubeconfig, "--kubeconfig-out", out,
                          "--timeout", "25m"], timeout=1800)


# --- the agent ------------------------------------------------------------------


@dataclasses.dataclass
class Config:
    repo: str
    branch: str
    state_namespace: str
    state_name: str
    management_clusters: frozenset
    backups: dict  # cluster -> (namespace, cronjob)


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
        self.plan_pulls()
        self.apply_main()

    # 1. plan ---------------------------------------------------------------------

    def plan_pulls(self):
        for pull in self.github.open_pulls():
            head = pull["head"]
            if (head.get("repo") or {}).get("full_name") != self.config.repo:
                continue  # forks never reach the management cluster
            if pull["base"]["ref"] != self.config.branch:
                continue
            changed, removed = [], []
            for f in self.github.pull_files(pull["number"]):
                match = HYDRA_YAML.match(f["filename"])
                if match:
                    (removed if f["status"] == "removed" else changed).append(match.group(1))
            if not changed and not removed:
                continue
            existing = self.github.plan_comment(pull["number"])
            meta = comment_meta(existing["body"]) if existing else None
            if meta and meta.get("head") == head["sha"]:
                continue  # already planned for this head
            log(f"planning PR #{pull['number']} at {head['sha'][:12]}: {', '.join(sorted(changed + removed))}")
            plans = []
            for cluster in sorted(changed):
                content = self.github.file_at(f"clusters/{cluster}/hydra.yaml", head["sha"])
                plans.append(self.hydra.plan(cluster, content))
            body = plan_comment(head["sha"], plans, sorted(removed), self.hydra.version)
            self.github.put_plan_comment(pull["number"], body, existing)
            if any(p.code != 0 for p in plans):
                state, desc = "error", "hydra plan failed; see the PR comment"
            elif any(not p.ok for p in plans):
                state, desc = "failure", "apply would refuse; see the ! lines in the PR comment"
            else:
                state, desc = "success", "plan posted; applies on merge if unchanged"
            self.github.status(head["sha"], "hydra/plan", state, desc)

    # 2. apply ---------------------------------------------------------------------

    def apply_main(self):
        clusters = self.github.cluster_dirs(self.config.branch)
        # Workload clusters before management clusters (ADR-002, PET-102).
        ordered = sorted(clusters, key=lambda c: (c in self.config.management_clusters, c))
        state = self.kube.get_state(self.config.state_namespace, self.config.state_name)
        data = state.setdefault("data", {}) or {}
        state["data"] = data
        changed = False
        workload_failed = False
        for cluster in ordered:
            if cluster in self.config.management_clusters and workload_failed:
                log(f"{cluster}: not applied this run, because a workload cluster failed")
                continue
            ok, touched = self.apply_cluster(cluster, data)
            changed |= touched
            if not ok and cluster not in self.config.management_clusters:
                workload_failed = True
        if changed:
            self.kube.put_state(state)

    def apply_cluster(self, cluster, data):
        """Returns (ok, state changed)."""
        path = f"clusters/{cluster}/hydra.yaml"
        sha = self.github.latest_commit(path, self.config.branch)
        if not sha or data.get(f"{cluster}.applied") == sha or data.get(f"{cluster}.blocked") == sha:
            return True, False
        if data.get(f"{cluster}.pending") == sha and self.now() < float(data.get(f"{cluster}.retry-after", "0")):
            return False, False  # failed before; waiting out the backoff
        content = self.github.file_at(path, sha)
        plan = self.hydra.plan(cluster, content)
        if plan.no_changes:
            log(f"{cluster}: {sha[:12]} needs no changes; recorded as applied")
            self._applied(cluster, sha, data)
            return True, True
        pull = self.github.merged_pull(sha)
        reason = None
        if plan.code != 0:
            reason = None  # transient until proven otherwise: retried below
        elif not plan.ok:
            reason = "the plan has refusals or conflicts (`!` lines)"
        elif pull is None:
            reason = "it did not come from a merged pull request, so no reviewed plan exists"
        else:
            existing = self.github.plan_comment(pull["number"])
            meta = comment_meta(existing["body"]) if existing else None
            planned = (meta or {}).get("plans", {}).get(cluster)
            if planned is None:
                reason = f"PR #{pull['number']} has no hydra-deployer plan for this cluster"
            elif planned != plan.digest:
                reason = f"the plan changed since PR #{pull['number']} was reviewed (something drifted)"
        if reason:
            self._block(cluster, sha, pull, reason, plan, data)
            return False, True
        if plan.code != 0:
            return self._failed(cluster, sha, pull, "hydra plan failed", plan.output, data)

        backup = self.config.backups.get(cluster)
        if backup:
            ok, job = self.kube.run_backup(*backup)
            if not ok:
                return self._failed(cluster, sha, pull, f"the etcd backup Job {backup[0]}/{job} did not succeed",
                                    "", data)
            log(f"{cluster}: etcd backup {backup[0]}/{job} succeeded")
        log(f"{cluster}: applying {sha[:12]} (PR #{pull['number']})")
        code, output = self.hydra.apply(cluster, content)
        if code != 0:
            return self._failed(cluster, sha, pull, "hydra cluster apply failed", output, data)
        self._applied(cluster, sha, data)
        self.github.status(sha, f"hydra/apply/{cluster}", "success", "applied")
        self.github.comment(pull["number"], f"<!-- hydra-apply -->\n**hydra-deployer applied `clusters/{cluster}/hydra.yaml` "
                                            f"at `{sha[:12]}`.**\n\n<details><summary>apply output</summary>\n\n"
                                            f"{fence(normalize(output).strip()[-20000:])}\n\n</details>")
        return True, True

    def _applied(self, cluster, sha, data):
        data[f"{cluster}.applied"] = sha
        for key in ("blocked", "pending", "failures", "retry-after"):
            data.pop(f"{cluster}.{key}", None)

    def _block(self, cluster, sha, pull, reason, plan, data):
        log(f"{cluster}: {sha[:12]} blocked: {reason}")
        self.github.status(sha, f"hydra/apply/{cluster}", "failure", f"not applied: {reason}")
        if pull:
            self.github.comment(pull["number"], f"**hydra-deployer did not apply `clusters/{cluster}/hydra.yaml` at "
                                                f"`{sha[:12]}`**: {reason}. Apply it by hand after checking "
                                                f"`hydra cluster plan`, or merge a follow-up change.\n\n"
                                                f"<details><summary>plan now</summary>\n\n"
                                                f"{fence(normalize(plan.output).strip()[-20000:])}\n\n</details>")
        data[f"{cluster}.blocked"] = sha
        for key in ("pending", "failures", "retry-after"):
            data.pop(f"{cluster}.{key}", None)

    def _failed(self, cluster, sha, pull, what, output, data):
        failures = int(data.get(f"{cluster}.failures", "0")) + 1 if data.get(f"{cluster}.pending") == sha else 1
        backoff = min(MAX_BACKOFF, 120 * 2 ** (failures - 1))
        data[f"{cluster}.pending"] = sha
        data[f"{cluster}.failures"] = str(failures)
        data[f"{cluster}.retry-after"] = str(self.now() + backoff)
        log(f"{cluster}: {sha[:12]} {what} (attempt {failures}); retrying in {backoff}s")
        if failures == 1:
            self.github.status(sha, f"hydra/apply/{cluster}", "error", f"{what}; retrying with backoff")
            if pull:
                self.github.comment(pull["number"], f"**hydra-deployer: {what}** for `clusters/{cluster}/hydra.yaml` at "
                                                    f"`{sha[:12]}`. It retries with backoff; rerunning is safe (stateless "
                                                    f"apply).\n\n<details><summary>output</summary>\n\n"
                                                    f"{fence(normalize(output).strip()[-20000:])}\n\n</details>")
        return False, True


# --- wiring ----------------------------------------------------------------------


def fetch_hydra(version, sha256, workdir, opener=urllib.request.urlopen):
    url = f"https://github.com/Petatron/hydra/releases/download/{version}/hydra-linux-amd64"
    path = os.path.join(workdir, "hydra")
    with opener(url, timeout=300) as resp:
        payload = resp.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != sha256:
        raise RuntimeError(f"hydra {version}: sha256 {digest} is not the pinned {sha256}")
    with open(path, "wb") as f:
        f.write(payload)
    os.chmod(path, 0o755)
    return path


def mgmt_kubeconfig(api_dir, server, workdir):
    """A kubeconfig for the management cluster from the mounted token and CA.
    Written to the pod's memory-backed work directory, never printed."""
    token = open(os.path.join(api_dir, "token")).read().strip()
    ca = base64.b64encode(open(os.path.join(api_dir, "ca.crt"), "rb").read()).decode()
    path = os.path.join(workdir, "management.kubeconfig")
    config = {
        "apiVersion": "v1", "kind": "Config", "current-context": "mgmt",
        "clusters": [{"name": "mgmt", "cluster": {"server": server, "certificate-authority-data": ca}}],
        "users": [{"name": "deployer", "user": {"token": token}}],
        "contexts": [{"name": "mgmt", "context": {"cluster": "mgmt", "user": "deployer"}}],
    }
    with open(path, "w") as f:
        json.dump(config, f)
    os.chmod(path, 0o600)
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


def main():
    env = os.environ
    workdir = env.get("WORKDIR") or tempfile.mkdtemp(prefix="hydra-deployer-")
    config = Config(
        repo=env["GITHUB_REPOSITORY"],
        branch=env.get("BRANCH", "main"),
        state_namespace=env["STATE_NAMESPACE"],
        state_name=env.get("STATE_CONFIGMAP", "hydra-deployer-state"),
        management_clusters=frozenset(filter(None, env.get("MANAGEMENT_CLUSTERS", "").split(","))),
        backups=parse_backups(env.get("BACKUP_CRONJOBS", "")),
    )
    token = open(env["GITHUB_TOKEN_FILE"]).read().strip()

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
