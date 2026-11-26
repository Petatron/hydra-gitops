"""Tests for hydra-deployer's decisions, with GitHub, Kubernetes and hydra faked."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import deployer as d  # noqa: E402

REPO = "Petatron/hydra-gitops"
CHANGES = "management cluster: /k\n+ create HelmChartProxy default/c-x\n1 change(s).\n"
REFUSED = "management cluster: /k\n! refused HelmChartProxy default/c-cilium (no longer rendered)\n"
NONE = f"management cluster: /k\n{d.NO_CHANGES}\n"


class FakeGitHub:
    def __init__(self):
        self.pulls = []
        self.files = {}        # number -> [{"filename", "status"}]
        self.contents = {}     # (path, ref) -> text
        self.comments = {}     # number -> [body]
        self.statuses = []     # (sha, context, state)
        self.clusters = []
        self.latest = {}       # path -> sha
        self.merged = {}       # sha -> pull

    def open_pulls(self):
        return self.pulls

    def pull_files(self, number):
        return self.files.get(number, [])

    def file_at(self, path, ref):
        return self.contents.get((path, ref))

    def plan_comment(self, number):
        for i, body in enumerate(self.comments.get(number, [])):
            if body.startswith(d.MARKER) and d.comment_meta(body) is not None:
                return {"id": (number, i), "body": body}
        return None

    def put_plan_comment(self, number, body, existing):
        bodies = self.comments.setdefault(number, [])
        if existing:
            bodies[existing["id"][1]] = body
        else:
            bodies.append(body)

    def comment(self, number, body):
        self.comments.setdefault(number, []).append(body)

    def status(self, sha, context, state, description):
        self.statuses.append((sha, context, state))

    def cluster_dirs(self, ref):
        return self.clusters

    def latest_commit(self, path, branch):
        return self.latest.get(path)

    def merged_pull(self, sha):
        return self.merged.get(sha)


class FakeKube:
    def __init__(self, backup_ok=True):
        self.cm = {"metadata": {"name": "s", "namespace": "n"}, "data": {}}
        self.puts = 0
        self.backups = []
        self.backup_ok = backup_ok

    def get_state(self, namespace, name):
        return self.cm

    def put_state(self, cm):
        self.puts += 1
        self.cm = cm

    def run_backup(self, namespace, cronjob):
        self.backups.append((namespace, cronjob))
        return self.backup_ok, "job-1"


class FakeHydra:
    version = "v0.6.0"

    def __init__(self, plans, apply_code=0):
        self.plans = plans      # cluster -> output (code 0) or (code, output)
        self.apply_code = apply_code
        self.calls = []

    def plan(self, cluster, content):
        self.calls.append(("plan", cluster, content))
        out = self.plans[cluster]
        code, out = out if isinstance(out, tuple) else (0, out)
        return d.Plan(cluster, code, out)

    def apply(self, cluster, content):
        self.calls.append(("apply", cluster, content))
        return self.apply_code, "applied 12 objects\n"


def pull(number, sha, repo=REPO, base="main"):
    return {"number": number, "head": {"sha": sha, "repo": {"full_name": repo}}, "base": {"ref": base}}


def deployer(gh, kube, hydra, now=1000.0, mgmt=("hydra-mgmt",), backups=None):
    config = d.Config(REPO, "main", "n", "s", frozenset(mgmt),
                      backups if backups is not None else {"hydra-mgmt": ("hydra-backup", "etcd-backup-hydra-mgmt")})
    return d.Deployer(config, gh, kube, lambda: hydra, now=lambda: now)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.pulls = [pull(7, "a" * 40)]
        self.gh.files[7] = [{"filename": "clusters/c/hydra.yaml", "status": "modified"},
                            {"filename": "README.md", "status": "modified"}]
        self.gh.contents[("clusters/c/hydra.yaml", "a" * 40)] = "spec: new"

    def test_posts_plan_and_status(self):
        hydra = FakeHydra({"c": CHANGES})
        deployer(self.gh, FakeKube(), hydra).plan_pulls()
        body = self.gh.comments[7][0]
        self.assertIn(d.MARKER, body)
        self.assertIn("applies on merge", body)
        self.assertNotIn("management cluster:", body)  # the kubeconfig path is not published
        meta = d.comment_meta(body)
        self.assertEqual(meta["head"], "a" * 40)
        self.assertEqual(meta["plans"]["c"], d.plan_hash(CHANGES))
        self.assertEqual(self.gh.statuses, [("a" * 40, "hydra/plan", "success")])
        self.assertEqual(hydra.calls, [("plan", "c", "spec: new")])

    def test_same_head_is_not_planned_again(self):
        hydra = FakeHydra({"c": CHANGES})
        dep = deployer(self.gh, FakeKube(), hydra)
        dep.plan_pulls()
        dep.plan_pulls()
        self.assertEqual(len(hydra.calls), 1)
        self.assertEqual(len(self.gh.comments[7]), 1)

    def test_new_head_updates_the_same_comment(self):
        hydra = FakeHydra({"c": CHANGES})
        deployer(self.gh, FakeKube(), hydra).plan_pulls()
        self.gh.pulls = [pull(7, "b" * 40)]
        self.gh.contents[("clusters/c/hydra.yaml", "b" * 40)] = "spec: newer"
        deployer(self.gh, FakeKube(), hydra).plan_pulls()
        self.assertEqual(len(self.gh.comments[7]), 1)
        self.assertEqual(d.comment_meta(self.gh.comments[7][0])["head"], "b" * 40)

    def test_forks_and_unrelated_prs_are_skipped(self):
        self.gh.pulls = [pull(7, "a" * 40, repo="someone/fork"), pull(8, "c" * 40), pull(9, "e" * 40, base="other")]
        self.gh.files[8] = [{"filename": "apps/x.yaml", "status": "modified"}]
        self.gh.files[9] = [{"filename": "clusters/c/hydra.yaml", "status": "modified"}]
        hydra = FakeHydra({})
        deployer(self.gh, FakeKube(), hydra).plan_pulls()
        self.assertEqual(hydra.calls, [])
        self.assertEqual(self.gh.comments, {})

    def test_refusal_fails_the_status_and_is_not_approvable(self):
        hydra = FakeHydra({"c": REFUSED})
        deployer(self.gh, FakeKube(), hydra).plan_pulls()
        body = self.gh.comments[7][0]
        self.assertIn("blocked", body)
        self.assertNotIn("c", d.comment_meta(body)["plans"])  # a refused plan can never match on merge
        self.assertEqual(self.gh.statuses[-1][2], "failure")

    def test_plan_error_is_an_error_status(self):
        hydra = FakeHydra({"c": (1, "Error: connecting")})
        deployer(self.gh, FakeKube(), hydra).plan_pulls()
        self.assertIn("plan failed", self.gh.comments[7][0])
        self.assertEqual(self.gh.statuses[-1][2], "error")

    def test_removed_cluster_is_reported_not_deleted(self):
        self.gh.files[7] = [{"filename": "clusters/c/hydra.yaml", "status": "removed"}]
        hydra = FakeHydra({})
        deployer(self.gh, FakeKube(), hydra).plan_pulls()
        self.assertIn("never deletes a cluster", self.gh.comments[7][0])
        self.assertEqual(hydra.calls, [])

    def test_backticks_cannot_break_out_of_the_block(self):
        plan = d.Plan("c", 0, "```\n**injected**\n")
        body = d.plan_comment("a" * 40, [plan], [], "v0")
        self.assertEqual(body.count("```"), 2)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.clusters = ["hydra-wl0"]
        self.sha = "f" * 40
        self.gh.latest["clusters/hydra-wl0/hydra.yaml"] = self.sha
        self.gh.contents[("clusters/hydra-wl0/hydra.yaml", self.sha)] = "spec: merged"
        self.gh.merged[self.sha] = {"number": 7, "merged_at": "x"}

    def reviewed(self, number, cluster, output):
        meta = {"head": "a" * 40, "plans": {cluster: d.plan_hash(output)}}
        self.gh.comments.setdefault(number, []).append(f"{d.MARKER}\nplan\n<!-- hydra-plan-meta {d.json.dumps(meta)} -->")

    def test_matching_reviewed_plan_is_applied_and_recorded(self):
        self.reviewed(7, "hydra-wl0", CHANGES)
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": CHANGES})
        deployer(self.gh, kube, hydra).apply_main()
        self.assertIn(("apply", "hydra-wl0", "spec: merged"), hydra.calls)
        self.assertEqual(kube.cm["data"]["hydra-wl0.applied"], self.sha)
        self.assertIn((self.sha, "hydra/apply/hydra-wl0", "success"), self.gh.statuses)
        self.assertEqual(kube.backups, [])  # only the management cluster is backed up

    def test_already_applied_does_nothing(self):
        kube, hydra = FakeKube(), FakeHydra({})
        kube.cm["data"]["hydra-wl0.applied"] = self.sha
        deployer(self.gh, kube, hydra).apply_main()
        self.assertEqual(hydra.calls, [])
        self.assertEqual(kube.puts, 0)

    def test_no_changes_is_recorded_without_applying(self):
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": NONE})
        del self.gh.merged[self.sha]  # even with no reviewed plan: nothing to apply
        deployer(self.gh, kube, hydra).apply_main()
        self.assertEqual([c[0] for c in hydra.calls], ["plan"])
        self.assertEqual(kube.cm["data"]["hydra-wl0.applied"], self.sha)

    def test_drift_blocks_once(self):
        self.reviewed(7, "hydra-wl0", CHANGES)
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": CHANGES + "+ create Something default/else\n"})
        deployer(self.gh, kube, hydra).apply_main()
        self.assertNotIn("apply", [c[0] for c in hydra.calls])
        self.assertEqual(kube.cm["data"]["hydra-wl0.blocked"], self.sha)
        self.assertIn("drifted", self.gh.comments[7][-1])
        hydra.calls.clear()
        deployer(self.gh, kube, hydra).apply_main()
        self.assertEqual(hydra.calls, [])  # not planned or reported again

    def test_refusal_blocks(self):
        self.reviewed(7, "hydra-wl0", REFUSED)
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": REFUSED})
        deployer(self.gh, kube, hydra).apply_main()
        self.assertNotIn("apply", [c[0] for c in hydra.calls])
        self.assertEqual(kube.cm["data"]["hydra-wl0.blocked"], self.sha)

    def test_direct_push_without_a_pull_request_blocks(self):
        del self.gh.merged[self.sha]
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": CHANGES})
        deployer(self.gh, kube, hydra).apply_main()
        self.assertNotIn("apply", [c[0] for c in hydra.calls])
        self.assertIn((self.sha, "hydra/apply/hydra-wl0", "failure"), self.gh.statuses)

    def test_missing_reviewed_plan_blocks(self):
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": CHANGES})
        deployer(self.gh, kube, hydra).apply_main()
        self.assertNotIn("apply", [c[0] for c in hydra.calls])
        self.assertIn("no hydra-deployer plan", self.gh.comments[7][-1])

    def test_failed_apply_backs_off_and_retries(self):
        self.reviewed(7, "hydra-wl0", CHANGES)
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": CHANGES}, apply_code=1)
        deployer(self.gh, kube, hydra, now=1000.0).apply_main()
        data = kube.cm["data"]
        self.assertEqual((data["hydra-wl0.pending"], data["hydra-wl0.failures"]), (self.sha, "1"))
        self.assertEqual(float(data["hydra-wl0.retry-after"]), 1120.0)
        comments = len(self.gh.comments[7])
        hydra.calls.clear()
        deployer(self.gh, kube, hydra, now=1100.0).apply_main()  # inside the backoff
        self.assertEqual(hydra.calls, [])
        deployer(self.gh, kube, hydra, now=1200.0).apply_main()  # after it
        self.assertIn(("apply", "hydra-wl0", "spec: merged"), hydra.calls)
        self.assertEqual(kube.cm["data"]["hydra-wl0.failures"], "2")
        self.assertEqual(float(kube.cm["data"]["hydra-wl0.retry-after"]), 1440.0)
        self.assertEqual(len(self.gh.comments[7]), comments)  # reported once, not every retry
        hydra.apply_code = 0
        deployer(self.gh, kube, hydra, now=1500.0).apply_main()
        self.assertEqual(kube.cm["data"].get("hydra-wl0.applied"), self.sha)
        self.assertNotIn("hydra-wl0.failures", kube.cm["data"])

    def test_management_cluster_last_and_after_a_backup(self):
        mgmt_sha = "e" * 40
        self.gh.clusters = ["hydra-mgmt", "hydra-wl0"]
        self.gh.latest["clusters/hydra-mgmt/hydra.yaml"] = mgmt_sha
        self.gh.contents[("clusters/hydra-mgmt/hydra.yaml", mgmt_sha)] = "spec: mgmt"
        self.gh.merged[mgmt_sha] = {"number": 8, "merged_at": "x"}
        self.reviewed(7, "hydra-wl0", CHANGES)
        self.reviewed(8, "hydra-mgmt", CHANGES)
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": CHANGES, "hydra-mgmt": CHANGES})
        deployer(self.gh, kube, hydra).apply_main()
        applies = [c[1] for c in hydra.calls if c[0] == "apply"]
        self.assertEqual(applies, ["hydra-wl0", "hydra-mgmt"])
        self.assertEqual(kube.backups, [("hydra-backup", "etcd-backup-hydra-mgmt")])

    def test_failed_backup_stops_the_management_apply(self):
        mgmt_sha = "e" * 40
        self.gh.clusters = ["hydra-mgmt"]
        self.gh.latest["clusters/hydra-mgmt/hydra.yaml"] = mgmt_sha
        self.gh.contents[("clusters/hydra-mgmt/hydra.yaml", mgmt_sha)] = "spec: mgmt"
        self.gh.merged[mgmt_sha] = {"number": 8, "merged_at": "x"}
        self.reviewed(8, "hydra-mgmt", CHANGES)
        kube, hydra = FakeKube(backup_ok=False), FakeHydra({"hydra-mgmt": CHANGES})
        deployer(self.gh, kube, hydra).apply_main()
        self.assertNotIn("apply", [c[0] for c in hydra.calls])
        self.assertEqual(kube.cm["data"]["hydra-mgmt.pending"], mgmt_sha)

    def test_failed_workload_holds_back_the_management_cluster(self):
        mgmt_sha = "e" * 40
        self.gh.clusters = ["hydra-mgmt", "hydra-wl0"]
        self.gh.latest["clusters/hydra-mgmt/hydra.yaml"] = mgmt_sha
        self.gh.contents[("clusters/hydra-mgmt/hydra.yaml", mgmt_sha)] = "spec: mgmt"
        self.gh.merged[mgmt_sha] = {"number": 8, "merged_at": "x"}
        self.reviewed(7, "hydra-wl0", CHANGES)
        self.reviewed(8, "hydra-mgmt", CHANGES)
        kube, hydra = FakeKube(), FakeHydra({"hydra-wl0": CHANGES, "hydra-mgmt": CHANGES}, apply_code=1)
        deployer(self.gh, kube, hydra).apply_main()
        self.assertEqual([c[1] for c in hydra.calls if c[0] == "apply"], ["hydra-wl0"])
        self.assertEqual(kube.backups, [])


class WiringTests(unittest.TestCase):
    def test_parse_backups(self):
        self.assertEqual(d.parse_backups("hydra-mgmt=hydra-backup/etcd-backup-hydra-mgmt, "),
                         {"hydra-mgmt": ("hydra-backup", "etcd-backup-hydra-mgmt")})
        with self.assertRaises(ValueError):
            d.parse_backups("hydra-mgmt")

    def test_fetch_hydra_checks_the_digest(self):
        import io
        import tempfile

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            opener = lambda url, timeout: Resp(b"binary")  # noqa: E731
            want = d.hashlib.sha256(b"binary").hexdigest()
            path = d.fetch_hydra("v0.6.0", want, tmp, opener)
            self.assertTrue(os.access(path, os.X_OK))
            with self.assertRaises(RuntimeError):
                d.fetch_hydra("v0.6.0", "0" * 64, tmp, opener)


class PinTests(unittest.TestCase):
    """The agent runs the same hydra the validator renders with."""

    def test_hydra_pin_matches_the_validator(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        install = open(os.path.join(root, "scripts/ci/install-tools.sh")).read()
        sums = open(os.path.join(root, "scripts/ci/tool-checksums.txt")).read()
        manifest = open(os.path.join(root, "deployer/manifests/hlcluster.yaml")).read()
        version = d.re.search(r"Petatron/hydra/releases/download/(v[0-9.]+)'", install).group(1)
        digest = d.re.search(r"^([a-f0-9]{64})\s+hydra-linux-amd64$", sums, d.re.M).group(1)

        def env(name):
            return d.re.search(rf"- name: {name}\n\s+value: (\S+)", manifest).group(1)

        self.assertEqual(env("HYDRA_VERSION"), version)
        self.assertEqual(env("HYDRA_SHA256"), digest)


if __name__ == "__main__":
    unittest.main()
