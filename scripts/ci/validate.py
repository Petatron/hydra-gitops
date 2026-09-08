#!/usr/bin/env python3
"""Validate all repository YAML/JSON and rendered Kustomize resources, without a cluster."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

REPO = Path(__file__).resolve().parents[2]
KUBERNETES_VERSION = "1.35.3"
K8S_SCHEMAS = "07b64c5376535fbbd6fb9910621e1a41f7613c14"
CRD_SCHEMAS = "866b2653a5334db9aed20ad74701e20fd464471b"
KUSTOMIZATIONS = {"kustomization.yaml", "kustomization.yml", "Kustomization"}


class Invalid(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """Fail on duplicate keys instead of silently accepting the last value."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise Invalid("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse(text, label):
    try:
        return list(yaml.load_all(text, Loader=UniqueLoader))
    except (yaml.YAMLError, Invalid, TypeError, RecursionError) as error:
        # Do not echo input snippets: the bad input may itself contain a credential.
        raise Invalid(f"{label}: YAML parse failed ({type(error).__name__})") from error


def pinned_image(image):
    if not isinstance(image, str):
        return False
    if "@" in image:
        return bool(re.fullmatch(r"[^\s@]+@sha256:[a-fA-F0-9]{64}", image))
    name, separator, tag = image.rsplit("/", 1)[-1].partition(":")
    return bool(name and separator and tag and tag.lower() != "latest")


def application_paths(resource, root, label):
    spec = resource.get("spec", {})
    sources = list(spec.get("sources") or [])
    if spec.get("source"):
        sources.append(spec["source"])
    for source in sources:
        if "path" not in source:
            continue  # Helm chart sources and values-only refs have no local path.
        path = source["path"]
        if not isinstance(path, str) or not path:
            raise Invalid(f"{label}: Application path must be nonempty")
        resolved = (root / path).resolve()
        if Path(path).is_absolute() or not resolved.is_relative_to(root):
            raise Invalid(f"{label}: Application path escapes repository")
        if "management-cluster" in Path(path).parts or "management-cluster" in resolved.parts:
            raise Invalid(f"{label}: Application must not reference management-cluster/")
        if not resolved.is_dir():
            raise Invalid(f"{label}: Application path does not exist: {path}")
        # Also prevent pointing at an ancestor that could sweep management RBAC in.
        if any(p.is_dir() for p in resolved.rglob("management-cluster")):
            raise Invalid(f"{label}: Application path contains management-cluster/")


def inspect_document(value, root, label, resources):
    if isinstance(value, dict) and ("kind" in value or "apiVersion" in value):
        if not isinstance(value.get("kind"), str) or not isinstance(value.get("apiVersion"), str):
            raise Invalid(f"{label}: resource must have both apiVersion and kind")
    inspect(value, root, label, resources)


def inspect(value, root, label, resources, ancestors=None):
    ancestors = set() if ancestors is None else ancestors
    if not isinstance(value, (dict, list)):
        return
    if id(value) in ancestors:
        raise Invalid(f"{label}: recursive YAML alias")
    ancestors = ancestors | {id(value)}
    if isinstance(value, list):
        for item in value:
            inspect(item, root, label, resources, ancestors)
        return
    kind = value.get("kind")
    api = value.get("apiVersion")
    if kind == "Secret" and ("data" in value or "stringData" in value):
        raise Invalid(f"{label}: Secret must not contain data or stringData")
    if kind == "Application" and value.get("apiVersion", "").startswith("argoproj.io/"):
        application_paths(value, root, label)
    if "image" in value and isinstance(value["image"], str) and not pinned_image(value["image"]):
        raise Invalid(f"{label}: image must have an explicit non-latest tag or sha256 digest")
    is_kustomize = isinstance(api, str) and api.startswith("kustomize.config.k8s.io/")
    if isinstance(kind, str) and isinstance(api, str) and not is_kustomize and (api, kind) != ("v1", "List"):
        resources.append(value)
    for child in value.values():
        inspect(child, root, label, resources, ancestors)
    if kind == "ConfigMap":
        for name, content in (value.get("data") or {}).items():
            if isinstance(content, str) and name.endswith((".yaml", ".yml", ".json")):
                for embedded in parse(content, f"{label}:{name}"):
                    inspect_document(embedded, root, f"{label}:{name}", resources)


def schema_validate(resources, label):
    if not resources:
        return
    with tempfile.TemporaryDirectory(prefix="gitops-schema-") as tmp:
        manifest = Path(tmp) / "resources.json"
        manifest.write_text("\n---\n".join(json.dumps(r) for r in resources))
        command = [
            "kubeconform", "-strict", "-summary", "-kubernetes-version", KUBERNETES_VERSION,
            "-schema-location", str(REPO / "schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"),
            "-schema-location", f"https://raw.githubusercontent.com/yannh/kubernetes-json-schema/{K8S_SCHEMAS}/"
            "{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json",
            "-schema-location", f"https://raw.githubusercontent.com/datreeio/CRDs-catalog/{CRD_SCHEMAS}/"
            "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
            str(manifest),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise Invalid(f"{label}: Kubernetes schema validation failed\n{result.stdout}{result.stderr}")
        print(f"{label}: {result.stdout.strip()}")


def validate(root):
    root = root.resolve()
    # Include new files locally, and committed files even when .gitignore matches.
    result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=root, capture_output=True, text=True, check=True)
    paths = sorted({root / name for name in result.stdout.split("\0") if name})
    builds = set()
    for path in paths:
        if not path.exists():  # locally deleted, not staged yet
            continue
        if path.name not in KUSTOMIZATIONS and path.suffix.lower() not in {".yaml", ".yml", ".json"}:
            continue
        if not path.resolve().is_relative_to(root):
            raise Invalid(f"{path.relative_to(root)}: file symlink escapes repository")
        label = str(path.relative_to(root))
        resources = []
        for document in parse(path.read_text(), label):
            inspect_document(document, root, label, resources)
        schema_validate(resources, label)
        if path.name in KUSTOMIZATIONS:
            builds.add(path.parent)
    for directory in sorted(builds):
        label = f"kustomize build {directory.relative_to(root)}"
        result = subprocess.run(["kustomize", "build", str(directory)], capture_output=True, text=True)
        if result.returncode:
            raise Invalid(f"{label}: failed\n{result.stderr}")
        resources = []
        for document in parse(result.stdout, label):
            inspect_document(document, root, label, resources)
        schema_validate(resources, label)
    print(f"GitOps validation passed; built {len(builds)} Kustomize directories.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=REPO)
    args = parser.parse_args()
    try:
        validate(args.root)
    except (Invalid, OSError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
