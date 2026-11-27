"""Tests for hydra_deploy's decisions: what is planned, and when apply runs."""

import unittest

import hydra_deploy as hd

WL0 = """apiVersion: hydra.io/v1alpha1
kind: ClusterConfig
metadata:
  name: hydra-wl0
spec: {}
"""

# Real output of `hydra cluster plan` (v0.6.0) against hydra-mgmt, 2026-11-27:
# a scratch hydra.yaml for hydra-wl0 with the compute pool's max raised 6 -> 7.
PLAN_CHANGES = """~ update MachineDeployment default/hydra-wl0-compute
--- live
+++ rendered
@@ -4,5 +4,5 @@
   annotations:
     capacity.cluster-autoscaler.kubernetes.io/labels: hydra.petatron.io/class=4c8g,hydra.petatron.io/pool=compute
-    cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size: "6"
+    cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size: "7"
     cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size: "0"
     machinedeployment.clusters.x-k8s.io/revision: "1"

1 change(s).
"""
PLAN_NONE = "No changes: the live cluster matches hydra.yaml.\n"  # real, same day
REPO = "Petatron/hydra-gitops"


def plan(text, status=None, exit_code=0, yaml_text=WL0):
    return {"yaml_sha256": hd.sha256(yaml_text), "plan": text, "exit": exit_code,
            "status": status or hd.classify(text, exit_code)}


def ancestry(*chain):
    """is_ancestor for a linear history given oldest first."""
    pos = {c: i for i, c in enumerate(chain)}
    return lambda a, b: a in pos and b in pos and pos[a] <= pos[b]


class PathsTests(unittest.TestCase):
    def test_cluster_of(self):
        self.assertEqual(hd.cluster_of("clusters/hydra-wl0/hydra.yaml"), "hydra-wl0")
        for p in ["clusters/hydra-wl0/apps/x.yaml", "clusters/hydra-wl0/hydra.yml",
                  "clusters/Bad/hydra.yaml", "clusters/a/b/hydra.yaml", "hydra.yaml",
                  "clusters/-x/hydra.yaml", "clusters/../hydra.yaml"]:
            self.assertIsNone(hd.cluster_of(p), p)

    def test_workloads_before_management(self):
        paths = ["clusters/hydra-mgmt/hydra.yaml", "README.md",
                 "clusters/hydra-wl0/hydra.yaml", "clusters/b/hydra.yaml", "clusters/b/hydra.yaml"]
        self.assertEqual(hd.changed_clusters(paths), ["b", "hydra-wl0", "hydra-mgmt"])


class IdentityTests(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(hd.identity_problem("hydra-wl0", WL0))
        self.assertIsNone(hd.identity_problem("hydra-wl0", WL0.replace("  name: hydra-wl0", "  name: hydra-wl0\n  namespace: default")))

    def test_refused(self):
        for text, want in [
            (WL0.replace("name: hydra-wl0", "name: hydra-mgmt"), "not its directory's name"),
            (WL0.replace("  name: hydra-wl0", "  name: hydra-wl0\n  namespace: other"), "live in 'default'"),
            (WL0 + "---\n" + WL0, "exactly one YAML document"),
            ("- a\n- b\n", "exactly one YAML document"),
            ("kind: x\n", "no metadata"),
            ("a: [", "not valid YAML"),
        ]:
            self.assertIn(want, hd.identity_problem("hydra-wl0", text) or "", text)


class ClassifyTests(unittest.TestCase):
    def test_real_output(self):
        self.assertEqual(hd.classify(PLAN_NONE, 0), "nochange")
        # The unified diff's own lines (---, +++, -x, +x) start at column 0;
        # the update marker alone already makes it a change.
        self.assertEqual(hd.classify(PLAN_CHANGES, 0), "changes")

    def test_markers(self):
        self.assertEqual(hd.classify("+ create HelmChartProxy default/x\n", 0), "changes")
        self.assertEqual(hd.classify("No changes to apply; 1 to prune\n- prune KubeadmConfigTemplate default/t (unused)\n", 0), "changes")
        self.assertEqual(hd.classify("No changes to apply; 1 kept\n? kept HydraMachineTemplate default/t (in use)\n", 0), "nochange")
        self.assertEqual(hd.classify("! refused proxy/x (...)\n" + PLAN_CHANGES, 0), "refused")
        # The one-time unprotected-roll note is a `!` line too: a person decides.
        self.assertEqual(hd.classify("! note: an unprotected roll\n" + PLAN_CHANGES, 0), "refused")
        self.assertEqual(hd.classify(PLAN_CHANGES, 1), "error")
        self.assertEqual(hd.classify("", 1), "error")

    def test_plan_state(self):
        self.assertEqual(hd.plan_state({"a": {"status": "nochange"}})[0], "success")
        self.assertEqual(hd.plan_state({"a": {"status": "changes"}, "b": {"status": "refused"}})[0], "failure")
        self.assertEqual(hd.plan_state({"a": {"status": "invalid"}})[0], "failure")
        self.assertEqual(hd.plan_state({"a": {"status": "changes"}, "b": {"status": "error"}})[0], "error")
        state, desc = hd.plan_state({"hydra-mgmt": {"status": "changes"}})
        self.assertEqual(state, "success")
        self.assertIn("by hand", desc)


class DecideTests(unittest.TestCase):
    def test_applies_only_the_reviewed_plan(self):
        rev = plan(PLAN_CHANGES)
        self.assertEqual(hd.decide("hydra-wl0", WL0, rev, plan(PLAN_CHANGES))[0], "apply")

    def test_every_reason_not_to(self):
        rev = plan(PLAN_CHANGES)
        other_yaml = WL0.replace("spec: {}", "spec: {x: 1}")
        cases = [
            ("removed file", None, rev, None, "deleted"),
            ("never planned", WL0, None, plan(PLAN_CHANGES), "blocked"),
            ("yaml changed after the plan", other_yaml, rev, plan(PLAN_CHANGES, yaml_text=other_yaml), "blocked"),
            ("plan differs now (drift)", WL0, rev, plan(PLAN_CHANGES + "~ update KubeadmControlPlane default/x\n"), "blocked"),
            ("plan of main failed", WL0, rev, plan("", exit_code=1), "error"),
            ("not planned now", WL0, rev, None, "error"),
            ("refusal", WL0, plan("! conflict x\n"), plan("! conflict x\n"), "blocked"),
            ("nothing to do", WL0, plan(PLAN_NONE), plan(PLAN_NONE), "nochange"),
            # Already applied (a re-processed range, or applied by hand): the
            # live cluster matches main, so there is nothing to block.
            ("reviewed changes, already applied", WL0, rev, plan(PLAN_NONE), "nochange"),
        ]
        for label, text, reviewed, now, want in cases:
            self.assertEqual(hd.decide("hydra-wl0", text, reviewed, now)[0], want, label)

    def test_wrong_identity_on_main_is_blocked(self):
        text = WL0.replace("name: hydra-wl0", "name: hydra-mgmt")
        rev = plan(PLAN_CHANGES, yaml_text=text)
        self.assertEqual(hd.decide("hydra-wl0", text, rev, plan(PLAN_CHANGES, yaml_text=text))[0], "blocked")

    def test_management_cluster_is_plan_only(self):
        mgmt = WL0.replace("hydra-wl0", "hydra-mgmt")
        rev = plan(PLAN_CHANGES, yaml_text=mgmt)
        self.assertEqual(hd.decide("hydra-mgmt", mgmt, rev, plan(PLAN_CHANGES, yaml_text=mgmt))[0], "planonly")
        rev = plan(PLAN_NONE, yaml_text=mgmt)
        self.assertEqual(hd.decide("hydra-mgmt", mgmt, rev, plan(PLAN_NONE, yaml_text=mgmt))[0], "nochange")


class TrustTests(unittest.TestCase):
    def test_candidate_artifacts(self):
        sha = "a" * 40
        name = hd.ARTIFACT_PREFIX + sha
        arts = [
            {"name": name, "created_at": "2026-11-27T01:00:00Z", "id": 1},
            {"name": name, "created_at": "2026-11-27T03:00:00Z", "id": 3, "expired": True},
            {"name": name, "created_at": "2026-11-27T02:00:00Z", "id": 2},
            {"name": name, "created_at": "2026-11-27T05:00:00Z", "id": 5},  # after the merge
            {"name": hd.ARTIFACT_PREFIX + "b" * 40, "created_at": "2026-11-27T04:00:00Z", "id": 4},
        ]
        got = [a["id"] for a in hd.candidate_artifacts(arts, sha, "2026-11-27T04:30:00Z")]
        self.assertEqual(got, [2, 1])  # newest first; never expired, foreign or post-merge
        self.assertEqual(hd.candidate_artifacts(arts, "c" * 40, "2026-11-27T04:30:00Z"), [])

    def test_trusted_run(self):
        good = {"path": hd.WORKFLOW_PATH, "event": "pull_request_target", "repository": {"full_name": REPO}}
        self.assertTrue(hd.trusted_run(good, REPO))
        for change in [{"event": "pull_request"}, {"event": "push"}, {"event": "workflow_dispatch"},
                       {"path": ".github/workflows/other.yaml"}, {"repository": {"full_name": "fork/hydra-gitops"}},
                       {"repository": None}]:
            self.assertFalse(hd.trusted_run({**good, **change}, REPO), change)

    def test_already_applied(self):
        mark = {"context": "hydra/apply/hydra-wl0", "creator": {"login": "github-actions[bot]"}, "state": "success"}
        self.assertTrue(hd.already_applied([mark], "hydra-wl0"))
        self.assertTrue(hd.already_applied([{**mark, "state": "pending"}], "hydra-wl0"))  # interrupted: a person decides
        self.assertFalse(hd.already_applied([mark], "hydra-wl1"))
        self.assertFalse(hd.already_applied([{**mark, "creator": {"login": "someone"}}], "hydra-wl0"))  # only the bot's
        self.assertFalse(hd.already_applied([{"context": "hydra/plan", "creator": {"login": "github-actions[bot]"}}], "hydra-wl0"))
        self.assertFalse(hd.already_applied([], "hydra-wl0"))

    def test_plans_match(self):
        pr = {"number": 7, "head": {"sha": "h" * 40}}
        good = {"repository": REPO, "pr": 7, "head_sha": "h" * 40}
        self.assertTrue(hd.plans_match(good, REPO, pr))
        for change in [{"pr": 8}, {"head_sha": "x" * 40}, {"repository": "o/r"}]:
            self.assertFalse(hd.plans_match({**good, **change}, REPO, pr), change)

    def test_pick_pr(self):
        # main: base -- c1 (squash of #3) -- c2 -- tip; #5 rebase-merged ending at c2
        isa = ancestry("base", "c1", "c2", "tip")
        merged = "2026-11-27T00:00:00Z"
        pulls = [
            {"number": 1, "merged_at": None, "merge_commit_sha": "c1", "base": {"ref": "main"}},
            {"number": 2, "merged_at": merged, "merge_commit_sha": "c1", "base": {"ref": "other"}},
            {"number": 3, "merged_at": merged, "merge_commit_sha": "c1", "base": {"ref": "main"}},
        ]
        self.assertEqual(hd.pick_pr(pulls, "c1", "tip", isa)["number"], 3)
        rebase = [{"number": 5, "merged_at": merged, "merge_commit_sha": "c2", "base": {"ref": "main"}}]
        self.assertEqual(hd.pick_pr(rebase, "c1", "tip", isa)["number"], 5)
        # A merge commit outside commit..tip is someone else's.
        stale = [{"number": 6, "merged_at": merged, "merge_commit_sha": "base", "base": {"ref": "main"}}]
        self.assertIsNone(hd.pick_pr(stale, "c1", "tip", isa))
        self.assertIsNone(hd.pick_pr([], "c1", "tip", isa))



class EvaluateTests(unittest.TestCase):
    """apply's per-cluster step at main's tip: plan first, look up the PR only
    when there is something to apply."""

    def finder(self, result):
        calls = []

        def find():
            calls.append(1)
            if isinstance(result, Exception):
                raise result
            return result
        return find, calls

    def test_no_changes_needs_no_pr(self):
        find, calls = self.finder(hd.Blocked("never asked"))
        self.assertEqual(hd.evaluate("hydra-wl0", WL0, plan(PLAN_NONE), find)[0], "nochange")
        self.assertEqual(calls, [])

    def test_changes_apply_the_reviewed_plan(self):
        find, calls = self.finder(plan(PLAN_CHANGES))
        self.assertEqual(hd.evaluate("hydra-wl0", WL0, plan(PLAN_CHANGES), find)[0], "apply")
        self.assertEqual(calls, [1])

    def test_changes_without_a_reviewed_plan_block(self):
        # Drift on a cluster nobody changed in git, or a direct push.
        find, _ = self.finder(hd.Blocked("no merged pull request behind abc"))
        action, reason = hd.evaluate("hydra-wl0", WL0, plan(PLAN_CHANGES), find)
        self.assertEqual((action, reason), ("blocked", "no merged pull request behind abc"))

    def test_identity_and_plan_errors_come_first(self):
        find, calls = self.finder(plan(PLAN_CHANGES))
        bad = WL0.replace("name: hydra-wl0", "name: hydra-mgmt")
        self.assertEqual(hd.evaluate("hydra-wl0", bad, None, find)[0], "blocked")
        self.assertEqual(hd.evaluate("hydra-wl0", WL0, plan("", exit_code=1), find)[0], "error")
        self.assertEqual(calls, [])


class CommentTests(unittest.TestCase):
    def test_fence_survives_backticks(self):
        body = hd.fence("a ```` b")
        self.assertTrue(body.startswith("`````\n"))
        self.assertTrue(body.endswith("\n`````"))

    def test_plan_comment(self):
        body = hd.plan_comment("f" * 40, {
            "hydra-wl0": plan(PLAN_CHANGES),
            "hydra-mgmt": plan(PLAN_NONE, yaml_text=WL0.replace("hydra-wl0", "hydra-mgmt")),
            "old": {"status": "deleted", "message": "hydra.yaml is removed."},
            "evil": {"status": "invalid", "message": "hydra.yaml has metadata.name '@someone <img src=x>'."},
        })
        self.assertTrue(body.startswith(hd.COMMENT_MARKER))
        self.assertIn("- `hydra-wl0`: Changes", body)
        self.assertIn("plan-only", body)
        self.assertIn("for reading only", body)
        # Text from the PR is shown only inside a code block.
        self.assertIn("```\nhydra.yaml has metadata.name '@someone <img src=x>'.\n```", body)

    def test_open_fence(self):
        self.assertEqual(hd.open_fence("a\n```diff\nx\n```\nb"), "")
        self.assertEqual(hd.open_fence("a\n`````\nx ``` y\n```\nz"), "`````")  # a shorter fence does not close
        self.assertEqual(hd.open_fence("```diff\n- a"), "```")

    def test_plan_comment_is_bounded_and_keeps_its_summary(self):
        body = hd.plan_comment("f" * 40, {"hydra-wl0": plan("~ x\n" * 40000), "b": plan("! refused y\n")})
        self.assertLess(len(body), hd.COMMENT_LIMIT + 200)
        self.assertIn("- `b`: **Refused**", body)  # the summary comes before any plan
        self.assertEqual(hd.open_fence(body), "")


if __name__ == "__main__":
    unittest.main()
