"""Tests for hydra-deployer's decisions, with GitHub, Kubernetes and hydra faked."""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))

import deployer as d  # noqa: E402

REPO = "Petatron/hydra-gitops"
CHANGES = "management cluster: /k\n+ create HelmChartProxy default/c-x\n1 change(s).\n"
REFUSED = "management cluster: /k\n! refused HelmChartProxy default/c-cilium (no longer rendered)\n"
NONE = f"management cluster: /k\n{d.NO_CHANGES}\n"
APPROVER = "Eyasluna"


def yaml(name, namespace=None, extra=""):
    ns = f"  namespace: {namespace}\n" if namespace else ""
    return f"apiVersion: hydra.io/v1alpha1\nkind: ClusterConfig\nmetadata:\n  name: {name}\n{ns}spec:\n  x: 1{extra}\n"


class FakeGitHub:
    def __init__(self):
        self.pulls = []
        self.files = {}        # number -> [{"filename", "status"}]
        self.contents = {}     # (path, ref) -> text
        self.comments = {}     # number -> {id: body}
        self.next_id = 100
        self.statuses = []     # (sha, context, state)
        self.main = "m" * 40
        self.clusters = []
        self.latest = {}       # path -> sha
        self.merged = {}       # sha -> pull
        self.reviews = {}      # number -> [review]

    def open_pulls(self):
        return self.pulls

    def pull_files(self, number):
        return self.files.get(number, [])

    def file_at(self, path, ref):
        return self.contents.get((path, ref))

    def put_comment(self, number, body, comment_id):
        bodies = self.comments.setdefault(number, {})
        if comment_id and int(comment_id) in bodies:
            bodies[int(comment_id)] = body
            return int(comment_id)
        self.next_id += 1
        bodies[self.next_id] = body
        return self.next_id

    def comment(self, number, body):
        self.put_comment(number, body, None)

    def status(self, sha, context, state, description):
        self.statuses.append((sha, context, state))

    def head(self, branch):
        return self.main

    def cluster_dirs(self, ref):
        assert ref == self.main
        return self.clusters

    def latest_commit(self, path, ref):
        assert ref == self.main
        return self.latest.get(path)

    def merged_pull(self, sha, branch):
        pull = self.merged.get(sha)
        return pull if pull and pull["base"]["ref"] == branch else None

    def approved(self, pull, approvers):
        return d.GitHub.approved(self, pull, approvers)

    def pages(self, path):  # used by GitHub.approved for reviews
        number = int(path.split("/")[2])
        return self.reviews.get(number, [])


class FakeKube:
    def __init__(self, backup_ok=True):
        self.cm = {"metadata": {"name": "s", "namespace": "n", "resourceVersion": "1"}, "data": {}}
        self.puts = 0
        self.backups = []
        self.backup_ok = backup_ok
        self.conflict_once = False

    def get_state(self, namespace, name):
        return json.loads(json.dumps(self.cm))

    def put_state(self, cm):
        if self.conflict_once:
            self.conflict_once = False
            self.cm["data"]["someone"] = "edited"
            raise urllib.error.HTTPError("u", 409, "Conflict", {}, None)
        self.puts += 1
        self.cm = json.loads(json.dumps(cm))
        return json.loads(json.dumps(cm))

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


def pull(number, sha, repo=REPO, base="main", merged_by=None):
    p = {"number": number, "head": {"sha": sha, "repo": {"full_name": repo}}, "base": {"ref": base}}
    if merged_by is not None:
        p.update(merged_at="t", merged_by={"login": merged_by})
    return p


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def deployer(gh, kube, hydra, clock=None, mgmt=("hydra-mgmt",), backups=None):
    config = d.Config(REPO, "main", "n", "s", frozenset(mgmt),
                      backups if backups is not None else {"hydra-mgmt": ("hydra-backup", "etcd-backup-hydra-mgmt")},
                      frozenset({APPROVER}))
    return d.Deployer(config, gh, kube, lambda: hydra, now=clock or Clock())


def state(kube):
    return d.State(kube, "n", "s")


class IdentityTests(unittest.TestCase):
    def test_reads_block_style_metadata(self):
        self.assertEqual(d.identity(yaml("hydra-wl0")), ("hydra-wl0", None))
        self.assertEqual(d.identity(yaml("hydra-wl0", "default")), ("hydra-wl0", "default"))
        self.assertEqual(d.identity("# c\nmetadata:\n  name: 'a-b'  # x\n"), ("a-b", None))

    def test_ignores_names_outside_metadata(self):
        self.assertEqual(d.identity("spec:\n  name: hydra-mgmt\nmetadata:\n  name: c\n"), ("c", None))

    def test_problems(self):
        self.assertIsNone(d.identity_problem("c", yaml("c")))
        self.assertIn("not the directory", d.identity_problem("foo", yaml("hydra-mgmt")))
        self.assertIn("only deploys to default", d.identity_problem("c", yaml("c", "other")))
        self.assertIn("None", d.identity_problem("c", "metadata: {name: c}\n"))  # flow style is refused


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.pulls = [pull(7, "a" * 40)]
        self.gh.files[7] = [{"filename": "clusters/c/hydra.yaml", "status": "modified"},
                            {"filename": "README.md", "status": "modified"}]
        self.gh.contents[("clusters/c/hydra.yaml", "a" * 40)] = yaml("c")
        self.kube = FakeKube()

    def run_plan(self, hydra, clock=None):
        s = state(self.kube)
        deployer(self.gh, self.kube, hydra, clock).plan_pulls(s)
        return s

    def test_posts_plan_status_and_records_it(self):
        hydra = FakeHydra({"c": CHANGES})
        self.run_plan(hydra)
        body = list(self.gh.comments[7].values())[0]
        self.assertIn(d.MARKER, body)
        self.assertIn("applies on merge", body)
        self.assertNotIn("management cluster:", body)  # the kubeconfig path is not published
        self.assertEqual(self.gh.statuses, [("a" * 40, "hydra/plan", "success")])
        record = json.loads(self.kube.cm["data"]["plan.7.c"])
        self.assertEqual(record, {"head": "a" * 40, "content": d.sha256(yaml("c")), "digest": d.plan_hash(CHANGES)})

    def test_same_head_is_not_planned_again(self):
        hydra = FakeHydra({"c": CHANGES})
        self.run_plan(hydra)
        self.run_plan(hydra)
        self.assertEqual(len(hydra.calls), 1)
        self.assertEqual(len(self.gh.comments[7]), 1)

    def test_new_head_updates_the_same_comment_and_record(self):
        hydra = FakeHydra({"c": CHANGES})
        self.run_plan(hydra)
        self.gh.pulls = [pull(7, "b" * 40)]
        self.gh.contents[("clusters/c/hydra.yaml", "b" * 40)] = yaml("c", extra="\n  y: 2")
        self.run_plan(hydra)
        self.assertEqual(len(self.gh.comments[7]), 1)
        self.assertEqual(json.loads(self.kube.cm["data"]["plan.7.c"])["head"], "b" * 40)

    def test_a_typed_comment_is_not_a_plan(self):
        # Anyone can comment; a comment that looks like the agent's changes nothing.
        self.gh.comments[7] = {1: d.MARKER + '\n<!-- hydra-plan-meta {"head": "' + "a" * 40 + '"} -->'}
        hydra = FakeHydra({"c": CHANGES})
        self.run_plan(hydra)
        self.assertEqual(len(hydra.calls), 1)  # still planned
        self.assertIn("plan.7.c", self.kube.cm["data"])

    def test_forks_and_unrelated_prs_are_skipped(self):
        self.gh.pulls = [pull(7, "a" * 40, repo="someone/fork"), pull(8, "c" * 40), pull(9, "e" * 40, base="other")]
        self.gh.files[8] = [{"filename": "apps/x.yaml", "status": "modified"}]
        self.gh.files[9] = [{"filename": "clusters/c/hydra.yaml", "status": "modified"}]
        hydra = FakeHydra({})
        self.run_plan(hydra)
        self.assertEqual(hydra.calls, [])
        self.assertEqual(self.gh.comments, {})

    def test_refusal_fails_the_status_and_records_nothing(self):
        hydra = FakeHydra({"c": REFUSED})
        self.run_plan(hydra)
        self.assertIn("blocked", list(self.gh.comments[7].values())[0])
        self.assertNotIn("plan.7.c", self.kube.cm["data"])
        self.assertEqual(self.gh.statuses[-1][2], "failure")

    def test_wrong_metadata_name_is_refused_without_running_hydra(self):
        self.gh.contents[("clusters/c/hydra.yaml", "a" * 40)] = yaml("hydra-mgmt")
        hydra = FakeHydra({})
        self.run_plan(hydra)
        self.assertEqual(hydra.calls, [])
        self.assertIn("not the directory", list(self.gh.comments[7].values())[0])
        self.assertEqual(self.gh.statuses[-1][2], "error")

    def test_plan_error_is_retried_after_a_while(self):
        clock = Clock()
        hydra = FakeHydra({"c": (1, "Error: connecting")})
        self.run_plan(hydra, clock)
        self.assertEqual(self.gh.statuses[-1][2], "error")
        self.run_plan(hydra, clock)
        self.assertEqual(len(hydra.calls), 1)  # not again straight away
        clock.t += d.REPLAN_AFTER_ERROR + 1
        hydra.plans["c"] = CHANGES
        self.run_plan(hydra, clock)
        self.assertEqual(len(hydra.calls), 2)
        self.assertEqual(self.gh.statuses[-1][2], "success")
        self.assertEqual(len(self.gh.comments[7]), 1)

    def test_removed_and_renamed_clusters_are_reported_not_deleted(self):
        self.gh.files[7] = [{"filename": "clusters/c/hydra.yaml", "status": "removed"},
                            {"filename": "clusters/new/hydra.yaml", "status": "renamed",
                             "previous_filename": "clusters/old/hydra.yaml"}]
        self.gh.contents[("clusters/new/hydra.yaml", "a" * 40)] = yaml("new")
        hydra = FakeHydra({"new": CHANGES})
        self.run_plan(hydra)
        body = list(self.gh.comments[7].values())[0]
        self.assertIn("clusters/c/hydra.yaml`** is removed", body)
        self.assertIn("clusters/old/hydra.yaml`** is removed", body)

    def test_one_bad_pr_does_not_stop_the_others(self):
        self.gh.pulls = [pull(6, "9" * 40), pull(7, "a" * 40)]
        self.gh.files[6] = [{"filename": "clusters/c/hydra.yaml", "status": "modified"}]  # file unreadable
        hydra = FakeHydra({"c": CHANGES})
        self.run_plan(hydra)
        self.assertIn("plan.7.c", self.kube.cm["data"])
        self.assertIn("could not be read", list(self.gh.comments[6].values())[0])

    def test_old_plans_of_closed_prs_are_forgotten(self):
        clock = Clock()
        hydra = FakeHydra({"c": CHANGES})
        self.run_plan(hydra, clock)
        self.gh.pulls = []
        clock.t += d.PLAN_RECORD_TTL - 10
        self.run_plan(hydra, clock)
        self.assertIn("plan.7.c", self.kube.cm["data"])  # kept long enough to apply a merge
        clock.t += 20
        self.run_plan(hydra, clock)
        self.assertFalse([k for k in self.kube.cm["data"] if k.startswith("plan.7.")])

    def test_nothing_hides_in_the_fence(self):
        plan = d.Plan("c", 0, "```\n**injected** <!-- hidden -->\n")
        body = d.plan_comment("a" * 40, [plan], [], "v0")
        self.assertEqual(body.count("```"), 2)
        self.assertEqual(body.count("<!--"), 1)  # only the marker


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.clusters = ["hydra-wl0"]
        self.sha = "f" * 40
        self.head = "a" * 40
        self.content = yaml("hydra-wl0")
        self.gh.latest["clusters/hydra-wl0/hydra.yaml"] = self.sha
        self.gh.contents[("clusters/hydra-wl0/hydra.yaml", self.sha)] = self.content
        self.gh.merged[self.sha] = pull(7, self.head, merged_by=APPROVER)
        self.kube = FakeKube()

    def planned(self, number, cluster, output, head=None, content=None):
        self.kube.cm["data"][f"plan.{number}.{cluster}"] = json.dumps(
            {"head": head or self.head, "content": d.sha256(content or self.content), "digest": d.plan_hash(output)})
        self.kube.cm["data"][f"plan.{number}.at"] = "1000"

    def run_apply(self, hydra, clock=None):
        deployer(self.gh, self.kube, hydra, clock).apply_main(state(self.kube))

    def applies(self, hydra):
        return [c[1] for c in hydra.calls if c[0] == "apply"]

    def test_planned_approved_merge_is_applied_and_recorded(self):
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), ["hydra-wl0"])
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.applied"], self.sha)
        self.assertIn((self.sha, "hydra/apply/hydra-wl0", "success"), self.gh.statuses)
        self.assertEqual(self.kube.backups, [])  # only the management cluster is backed up

    def test_approval_by_review_at_the_final_head(self):
        self.gh.merged[self.sha] = pull(7, self.head, merged_by="someone")
        self.gh.reviews[7] = [{"state": "APPROVED", "commit_id": "0" * 40, "user": {"login": APPROVER}}]
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])  # approved an older head only
        self.assertIn("neither merged nor approved", list(self.gh.comments[7].values())[-1])
        del self.kube.cm["data"]["hydra-wl0.blocked"]
        self.gh.reviews[7].append({"state": "APPROVED", "commit_id": self.head, "user": {"login": APPROVER}})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), ["hydra-wl0"])

    def test_merged_head_must_be_the_planned_head(self):
        self.planned(7, "hydra-wl0", CHANGES, head="b" * 40)
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertIn("had not planned", list(self.gh.comments[7].values())[-1])

    def test_merged_content_must_be_the_planned_content(self):
        # Same plan text, different file: still not applied.
        self.planned(7, "hydra-wl0", CHANGES, content=yaml("hydra-wl0", extra="\n  other: 1"))
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertIn("not the one planned", list(self.gh.comments[7].values())[-1])

    def test_already_applied_does_nothing(self):
        hydra = FakeHydra({})
        self.kube.cm["data"]["hydra-wl0.applied"] = self.sha
        self.run_apply(hydra)
        self.assertEqual(hydra.calls, [])
        self.assertEqual(self.kube.puts, 0)

    def test_no_changes_is_recorded_without_applying(self):
        del self.gh.merged[self.sha]  # even with no reviewed plan: nothing to apply
        hydra = FakeHydra({"hydra-wl0": NONE})
        self.run_apply(hydra)
        self.assertEqual([c[0] for c in hydra.calls], ["plan"])
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.applied"], self.sha)

    def test_wrong_metadata_name_blocks_before_planning(self):
        self.gh.contents[("clusters/hydra-wl0/hydra.yaml", self.sha)] = yaml("hydra-mgmt")
        hydra = FakeHydra({})
        self.run_apply(hydra)
        self.assertEqual(hydra.calls, [])
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.blocked"], self.sha)

    def test_drift_blocks_once(self):
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES + "+ create Something default/else\n"})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.blocked"], self.sha)
        self.assertIn("drifted", list(self.gh.comments[7].values())[-1])
        hydra.calls.clear()
        self.run_apply(hydra)
        self.assertEqual(hydra.calls, [])  # not planned or reported again

    def test_refusal_blocks(self):
        hydra = FakeHydra({"hydra-wl0": REFUSED})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.blocked"], self.sha)

    def test_direct_push_or_other_base_blocks(self):
        self.gh.merged[self.sha] = dict(pull(7, self.head, base="scratch", merged_by=APPROVER))
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertIn((self.sha, "hydra/apply/hydra-wl0", "failure"), self.gh.statuses)

    def test_missing_plan_record_blocks(self):
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertIn("no successful plan", list(self.gh.comments[7].values())[-1])

    def test_failed_apply_backs_off_and_retries(self):
        clock = Clock(1000.0)
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES}, apply_code=1)
        self.run_apply(hydra, clock)
        data = self.kube.cm["data"]
        self.assertEqual((data["hydra-wl0.pending"], data["hydra-wl0.failures"]), (self.sha, "1"))
        self.assertEqual(float(data["hydra-wl0.retry-after"]), 1120.0)
        comments = len(self.gh.comments[7])
        hydra.calls.clear()
        clock.t = 1100.0
        self.run_apply(hydra, clock)  # inside the backoff
        self.assertEqual(hydra.calls, [])
        clock.t = 1200.0
        self.run_apply(hydra, clock)  # after it
        self.assertEqual(self.applies(hydra), ["hydra-wl0"])
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.failures"], "2")
        self.assertEqual(float(self.kube.cm["data"]["hydra-wl0.retry-after"]), 1440.0)
        self.assertEqual(len(self.gh.comments[7]), comments)  # reported once, not every retry
        hydra.apply_code = 0
        clock.t = 1500.0
        self.run_apply(hydra, clock)
        self.assertEqual(self.kube.cm["data"].get("hydra-wl0.applied"), self.sha)
        self.assertNotIn("hydra-wl0.failures", self.kube.cm["data"])

    def test_applied_is_saved_before_the_result_comment(self):
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES})

        def broken(*a):
            raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
        self.gh.comment = broken
        self.run_apply(hydra)
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.applied"], self.sha)

    def test_state_conflict_keeps_both_writes(self):
        self.planned(7, "hydra-wl0", CHANGES)
        self.kube.conflict_once = True
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.kube.cm["data"]["hydra-wl0.applied"], self.sha)
        self.assertEqual(self.kube.cm["data"]["someone"], "edited")

    def mgmt(self, number=8, sha="e" * 40):
        self.gh.clusters = ["hydra-mgmt", "hydra-wl0"]
        self.gh.latest["clusters/hydra-mgmt/hydra.yaml"] = sha
        self.gh.contents[("clusters/hydra-mgmt/hydra.yaml", sha)] = yaml("hydra-mgmt")
        self.gh.merged[sha] = pull(number, "c" * 40, merged_by=APPROVER)
        self.planned(number, "hydra-mgmt", CHANGES, head="c" * 40, content=yaml("hydra-mgmt"))

    def test_management_cluster_last_and_after_a_backup(self):
        self.mgmt()
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES, "hydra-mgmt": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), ["hydra-wl0", "hydra-mgmt"])
        self.assertEqual(self.kube.backups, [("hydra-backup", "etcd-backup-hydra-mgmt")])

    def test_failed_backup_stops_the_management_apply(self):
        self.mgmt()
        self.gh.clusters = ["hydra-mgmt"]
        self.kube.backup_ok = False
        hydra = FakeHydra({"hydra-mgmt": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertEqual(self.kube.cm["data"]["hydra-mgmt.pending"], "e" * 40)

    def test_failed_workload_holds_back_the_management_cluster(self):
        self.mgmt()
        self.planned(7, "hydra-wl0", CHANGES)
        hydra = FakeHydra({"hydra-wl0": CHANGES, "hydra-mgmt": CHANGES}, apply_code=1)
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), ["hydra-wl0"])
        self.assertEqual(self.kube.backups, [])

    def test_a_blocked_workload_holds_back_the_management_cluster(self):
        self.mgmt()
        self.gh.clusters = ["a-broken", "hydra-mgmt"]
        self.gh.latest["clusters/a-broken/hydra.yaml"] = "1" * 40  # unreadable: identity refuses it
        hydra = FakeHydra({"hydra-mgmt": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), [])
        self.assertEqual(self.kube.cm["data"]["a-broken.blocked"], "1" * 40)

    def test_an_exception_in_one_cluster_is_contained(self):
        self.gh.clusters = ["a-raises", "hydra-wl0"]
        self.gh.latest["clusters/a-raises/hydra.yaml"] = "2" * 40
        self.planned(7, "hydra-wl0", CHANGES)

        original = self.gh.file_at

        def file_at(path, ref):
            if path.startswith("clusters/a-raises/"):
                raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
            return original(path, ref)
        self.gh.file_at = file_at
        hydra = FakeHydra({"hydra-wl0": CHANGES})
        self.run_apply(hydra)
        self.assertEqual(self.applies(hydra), ["hydra-wl0"])


class WiringTests(unittest.TestCase):
    def test_parse_backups(self):
        self.assertEqual(d.parse_backups("hydra-mgmt=hydra-backup/etcd-backup-hydra-mgmt, "),
                         {"hydra-mgmt": ("hydra-backup", "etcd-backup-hydra-mgmt")})
        with self.assertRaises(ValueError):
            d.parse_backups("hydra-mgmt")

    def test_fetch_hydra_checks_the_digest(self):
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

    def test_kubeconfig_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, value in (("token", "t"), ("ca.crt", "c")):
                with open(os.path.join(tmp, name), "w") as f:
                    f.write(value)
            path = d.mgmt_kubeconfig(tmp, "https://x:6443", tmp)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class PinTests(unittest.TestCase):
    """The agent runs the same hydra the validator renders with."""

    def test_hydra_pin_matches_the_validator(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        def read(path):
            with open(os.path.join(root, path)) as f:
                return f.read()
        install = read("scripts/ci/install-tools.sh")
        sums = read("scripts/ci/tool-checksums.txt")
        manifest = read("deployer/manifests/hlcluster.yaml")
        version = d.re.search(r"Petatron/hydra/releases/download/(v[0-9.]+)'", install).group(1)
        digest = d.re.search(r"^([a-f0-9]{64})\s+hydra-linux-amd64$", sums, d.re.M).group(1)

        def env(name):
            return d.re.search(rf"- name: {name}\n\s+value: (\S+)", manifest).group(1)

        self.assertEqual(env("HYDRA_VERSION"), version)
        self.assertEqual(env("HYDRA_SHA256"), digest)


if __name__ == "__main__":
    unittest.main()
