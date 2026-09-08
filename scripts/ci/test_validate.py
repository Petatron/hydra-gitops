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
        self.run_validation("Application path does not exist")

    def test_missing_multisource_path(self):
        resource = app()
        resource["spec"]["sources"] = [resource["spec"].pop("source"), {"path": "missing"}]
        self.write("app.yaml", resource)
        self.run_validation("Application path does not exist")

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
