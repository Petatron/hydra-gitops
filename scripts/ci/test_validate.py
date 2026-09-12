"""Black-box acceptance checks: each broken fixture must fail the real CLI."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

VALIDATOR = Path(__file__).with_name("validate.py")


def pod(image="busybox:1.37.0"):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "test"},
            "spec": {"containers": [{"name": "test", "image": image}]}}


def app(path="workloads"):
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": "test"}, "spec": {"project": "default",
            "source": {"repoURL": "https://github.com/Petatron/hydra-gitops.git",
                       "targetRevision": "main", "path": path},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"}}}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("workloads/pod.yaml", pod())

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else yaml.safe_dump(content))

    def run_validation(self, expected=None):
        result = subprocess.run([sys.executable, str(VALIDATOR), str(self.root)],
                                capture_output=True, text=True)
        output = result.stdout + result.stderr
        if expected is None:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn(expected, output)

    def test_valid_native_and_crd_resources(self):
        self.write("app.yaml", app())
        self.write("pool.yaml", {"apiVersion": "metallb.io/v1beta1", "kind": "IPAddressPool",
                   "metadata": {"name": "test"}, "spec": {"addresses": ["192.0.2.1-192.0.2.5"]}})
        self.write("cilium.yaml", {"apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy",
                   "metadata": {"name": "test"}, "spec": {"endpointSelector": {}, "ingress": [{}]}})
        self.write("cluster.yaml", {"apiVersion": "cluster.x-k8s.io/v1beta2", "kind": "Cluster",
                   "metadata": {"name": "test"},
                   "spec": {"controlPlaneEndpoint": {"host": "192.0.2.10", "port": 6443}}})
        self.write("hydra.yaml", {"apiVersion": "infrastructure.cluster.x-k8s.io/v1alpha1",
                   "kind": "HydraCluster", "metadata": {"name": "test"},
                   "spec": {"controlPlaneEndpoint": {"host": "192.0.2.10", "port": 6443}}})
        self.run_validation()

    def test_invalid_yaml(self):
        self.write("broken.yaml", "spec: [unterminated")
        self.run_validation("YAML parse failed")

    def test_duplicate_yaml_key(self):
        self.write("broken.yml", "kind: Pod\nkind: Secret\n")
        self.run_validation("YAML parse failed")

    def test_yaml_merge_keys_and_explicit_override(self):
        self.write("merged.yaml", """apiVersion: v1
kind: ConfigMap
metadata:
  name: test
data:
  <<: [&first {mode: first, added: inherited}, {mode: second}]
  mode: override
""")
        self.run_validation()

    def test_duplicate_key_inside_merged_anchor(self):
        self.write("merged.yaml", "data:\n  <<: &base {mode: first, mode: second}\n")
        self.run_validation("YAML parse failed")

    def test_merge_keys_do_not_hide_explicit_duplicates(self):
        self.write("merged.yaml", "data:\n  <<: &base {mode: inherited}\n  mode: first\n  mode: second\n")
        self.run_validation("YAML parse failed")

    def test_yaml_dates_are_strings(self):
        self.write("dates.yaml", """apiVersion: v1
kind: ConfigMap
metadata:
  name: test
  creationTimestamp: 2026-01-01T00:00:00Z
data:
  day: 2026-01-01
""")
        self.run_validation()

    def test_invalid_kubernetes_schema(self):
        resource = pod()
        resource["spec"]["containers"][0]["ports"] = "wrong-type"
        self.write("broken.yaml", resource)
        self.run_validation("Kubernetes schema validation failed")

    def test_incomplete_resource_identity(self):
        resource = pod()
        del resource["apiVersion"]
        self.write("bad.yaml", resource)
        self.run_validation("resource must have both apiVersion and kind")

    def test_invalid_application_schema(self):
        resource = app()
        resource["spec"]["destination"] = 42
        self.write("broken.yaml", resource)
        self.run_validation("Kubernetes schema validation failed")

    def test_unknown_schema_fails_closed(self):
        self.write("unknown.yaml", {"apiVersion": "unknown.example/v1", "kind": "Unknown",
                                   "metadata": {"name": "test"}})
        self.run_validation("Kubernetes schema validation failed")

    def test_invalid_custom_resource_schemas(self):
        cases = [
            ("cilium.io/v2", "CiliumNetworkPolicy", {"endpointSelector": 42, "ingress": [{}]}),
            ("metallb.io/v1beta1", "IPAddressPool", {"addresses": 42}),
            ("cluster.x-k8s.io/v1beta2", "Cluster", {"controlPlaneEndpoint": {"port": "bad"}}),
            ("infrastructure.cluster.x-k8s.io/v1alpha1", "HydraCluster",
             {"controlPlaneEndpoint": {"host": "192.0.2.10", "port": "bad"}}),
        ]
        for api, kind, spec in cases:
            with self.subTest(kind=kind):
                self.write("bad-crd.yaml", {"apiVersion": api, "kind": kind,
                           "metadata": {"name": "test"}, "spec": spec})
                self.run_validation("Kubernetes schema validation failed")

    def test_missing_application_path(self):
        self.write("app.yaml", app("typo"))
        self.run_validation("Application path is not an existing directory")

    def test_missing_multisource_path(self):
        resource = app()
        resource["spec"]["sources"] = [resource["spec"].pop("source"), app("missing")["spec"]["source"]]
        self.write("app.yaml", resource)
        self.run_validation("Application path is not an existing directory")

    def test_file_is_not_an_application_directory(self):
        self.write("app.yaml", app("workloads/pod.yaml"))
        self.run_validation("Application path is not an existing directory")

    def test_git_source_must_be_this_repository(self):
        for url in ["https://github.com/example/other.git", "https://github.com/Petatron/hydra-gitops-extra",
                    "https://github.com.evil.example/Petatron/hydra-gitops.git", None]:
            with self.subTest(url=url):
                resource = app()
                resource["spec"]["source"]["repoURL"] = url
                self.write("app.yaml", resource)
                self.run_validation("requires this repository's repoURL")

    def test_supported_self_repository_urls(self):
        for url in ["https://github.com/Petatron/hydra-gitops", "https://github.com/Petatron/hydra-gitops.git",
                    "git@github.com:Petatron/hydra-gitops.git", "ssh://git@github.com/Petatron/hydra-gitops.git"]:
            with self.subTest(url=url):
                resource = app()
                resource["spec"]["source"]["repoURL"] = url
                self.write("app.yaml", resource)
                self.run_validation()

    def test_git_source_must_track_main(self):
        for revision in ["HEAD", "feature", "v1.0.0", "a" * 40, None]:
            with self.subTest(revision=revision):
                resource = app()
                resource["spec"]["source"]["targetRevision"] = revision
                self.write("app.yaml", resource)
                self.run_validation("requires targetRevision: main")

    def test_multisource_repository_and_revision(self):
        for field, value in [("repoURL", "https://github.com/example/other"), ("targetRevision", "HEAD")]:
            with self.subTest(field=field):
                resource = app()
                other = app()["spec"]["source"]
                other[field] = value
                resource["spec"]["sources"] = [resource["spec"].pop("source"), other]
                self.write("app.yaml", resource)
                self.run_validation("Git source path requires")

    def test_workload_cannot_reference_home_or_other_cluster(self):
        for target in ["apps", "bootstrap", "infrastructure", "infrastructure/metallb",
                       "clusters/other/apps", "infrastructure/storage-unsafe", "clusters/hydra-wl0/../../apps"]:
            with self.subTest(target=target):
                (self.root / target).mkdir(parents=True, exist_ok=True)
                self.write("clusters/hydra-wl0/apps/bad.yaml", app(target))
                self.run_validation("crosses cluster boundary")

    def test_home_cannot_reference_workload_cluster(self):
        (self.root / "clusters/hydra-wl0/apps").mkdir(parents=True)
        for origin in ["apps", "bootstrap"]:
            with self.subTest(origin=origin):
                self.write(f"{origin}/bad.yaml", app("clusters/hydra-wl0/apps"))
                self.run_validation("crosses cluster boundary")
                (self.root / origin / "bad.yaml").unlink()

    def test_workload_own_and_shared_storage_paths_allowed(self):
        for target in ["clusters/hydra-wl0/apps", "infrastructure/storage", "infrastructure/storage/nested"]:
            with self.subTest(target=target):
                (self.root / target).mkdir(parents=True, exist_ok=True)
                self.write("clusters/hydra-wl0/root-app.yaml", app(target))
                self.run_validation()

    def test_symlink_cannot_cross_cluster_boundary(self):
        (self.root / "apps").mkdir()
        (self.root / "clusters/hydra-wl0").mkdir(parents=True)
        (self.root / "clusters/hydra-wl0/alias").symlink_to(self.root / "apps", target_is_directory=True)
        self.write("clusters/hydra-wl0/root-app.yaml", app("clusters/hydra-wl0/alias"))
        self.run_validation("crosses cluster boundary")

    def test_rendered_application_keeps_cluster_scope(self):
        (self.root / "apps").mkdir()
        self.write("clusters/hydra-wl0/apps/child.yaml", app("clusters/hydra-wl0/apps"))
        self.write("clusters/hydra-wl0/apps/kustomization.yaml", {"resources": ["child.yaml"], "patches": [
            {"target": {"kind": "Application", "name": "test"},
             "patch": "- op: replace\n  path: /spec/source/path\n  value: apps"}]})
        self.run_validation("crosses cluster boundary")

    def test_path_escape(self):
        self.write("app.yaml", app("../"))
        self.run_validation("Application path escapes repository")

    def test_management_path(self):
        self.write("clusters/test/management-cluster/rbac.yaml", {"apiVersion": "v1", "kind": "Namespace",
                   "metadata": {"name": "management"}})
        self.write("app.yaml", app("clusters/test/management-cluster"))
        self.run_validation("must not reference management-cluster/")

    def test_management_ancestor(self):
        self.write("clusters/test/management-cluster/rbac.yaml", "")
        self.write("app.yaml", app("clusters/test"))
        self.run_validation("contains management-cluster/")

    def test_secret_anywhere_including_json_list(self):
        for field in ["data", "stringData"]:
            with self.subTest(field=field):
                self.write("unusual/location/secret.json", json.dumps({"apiVersion": "v1", "kind": "List",
                           "items": [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test"},
                                      field: {"dummy": "not-a-real-credential"}}]}))
                self.run_validation("Secret must not contain")

    def test_empty_token_secret_is_allowed(self):
        self.write("token.yaml", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "test",
                   "annotations": {"kubernetes.io/service-account.name": "test"}},
                   "type": "kubernetes.io/service-account-token"})
        self.run_validation()

    def test_floating_images(self):
        for image in ["busybox", "busybox:latest", "registry.example:5000/busybox"]:
            with self.subTest(image=image):
                self.write("bad.yaml", pod(image))
                self.run_validation("image must have an explicit")

    def test_init_and_ephemeral_images(self):
        for field in ["initContainers", "ephemeralContainers"]:
            with self.subTest(field=field):
                resource = pod()
                resource["spec"][field] = [{"name": "debug", "image": "busybox:latest"}]
                self.write("bad.yaml", resource)
                self.run_validation("image must have an explicit")

    def test_digest_image(self):
        self.write("digest.yaml", pod("busybox@sha256:" + "a" * 64))
        self.run_validation()

    def test_embedded_helper_pod(self):
        self.write("config.yaml", {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test"},
                   "data": {"helperPod.yaml": yaml.safe_dump(pod("busybox"))}})
        self.run_validation("image must have an explicit")

    def helm_app(self, values, as_string=False):
        resource = app()
        resource["spec"]["source"] = {
            "repoURL": "https://helm.cilium.io/", "chart": "cilium", "targetRevision": "1.19.1",
            "helm": {"values": yaml.safe_dump(values)} if as_string else {"valuesObject": values}}
        self.write("app.yaml", resource)

    def test_helm_image_mapping_rejects_floating_tags(self):
        for tag in ["latest", "", None]:
            for as_string in [False, True]:
                with self.subTest(tag=tag, as_string=as_string):
                    self.helm_app({"controller": {"image": {"repository": "example/app", "tag": tag}}}, as_string)
                    self.run_validation("image tag/imageTag must be an explicit")

    def test_helm_image_tag_siblings(self):
        for field in ["tag", "imageTag"]:
            with self.subTest(field=field):
                self.helm_app({"controller": {"image": "example/app", field: "latest"}})
                self.run_validation("image tag/imageTag must be an explicit")

    def test_helm_tag_override_without_repository(self):
        self.helm_app({"image": {"tag": "latest"}})
        self.run_validation("image tag/imageTag must be an explicit")

    def test_pinned_helm_images_and_omitted_chart_defaults(self):
        for values in [{"image": {"repository": "example/app", "tag": "1.2.3"}},
                       {"image": "example/app", "imageTag": "1.2.3"},
                       {"image": {"repository": "example/app", "digest": "sha256:" + "a" * 64}}, {}]:
            with self.subTest(values=values):
                self.helm_app(values)
                self.run_validation()

    def test_helm_repository_override_needs_a_pin(self):
        self.helm_app({"image": {"repository": "example/app"}})
        self.run_validation("image repository override requires")

    def test_vm_image_objects_are_not_container_images(self):
        self.write("settings.yaml", {"image": {"url": "https://example.com/vm.qcow2", "checksum": "abc"}})
        self.run_validation()

    def test_valid_kustomization(self):
        self.write("workloads/kustomization.yaml", {"resources": ["pod.yaml"]})
        self.run_validation()

    def test_broken_kustomization(self):
        self.write("workloads/kustomization.yaml", {"resources": ["missing.yaml"]})
        self.run_validation("kustomize build workloads: failed")

    def test_rendered_output_is_checked(self):
        self.write("workloads/kustomization.yaml", {"resources": ["pod.yaml"],
                   "images": [{"name": "busybox", "newTag": "latest"}]})
        self.run_validation("image must have an explicit")

    def test_generated_secret_is_rejected(self):
        self.write("workloads/kustomization.yaml", {"secretGenerator": [
                   {"name": "test", "literals": ["dummy=not-a-real-credential"]}]})
        self.run_validation("Secret must not contain")


if __name__ == "__main__":
    unittest.main()
