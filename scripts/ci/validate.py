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
SHARED_PATHS = (Path("infrastructure/storage"),)
SELF_REPOSITORY = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"Petatron/hydra-gitops(?:\.git)?/?", re.IGNORECASE)


class Invalid(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate explicit keys while retaining YAML merge precedence."""

    def flatten_mapping(self, node):
        # Check before flattening: an explicit override of an inherited key is legal.
        # Anchors can be flattened more than once, so inspect each original node once.
        if not getattr(node, "keys_checked", False):
            node.keys_checked = True
            keys = set()
            for key_node, _ in node.value:
                key = "<<" if key_node.tag == "tag:yaml.org,2002:merge" else self.construct_object(key_node)
                if key in keys:
                    raise Invalid("duplicate YAML key")
                keys.add(key)
        super().flatten_mapping(node)


# Kubernetes YAML treats date-like scalars as strings, not Python date objects.
UniqueLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
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
    return bool(name and separator and pinned_tag(tag))


def pinned_tag(tag):
    return isinstance(tag, str) and tag.lower() != "latest" and bool(re.fullmatch(r"[\w][\w.-]{0,127}", tag, re.ASCII))


def image_fields(value, label):
    image = value.get("image")
    tags = [value[key] for key in ("tag", "imageTag") if key in value]
    # Common Helm shapes: image: {repository, tag/digest}, or image + imageTag.
    # Do not classify Hydra's structured VM image (url/checksum) as a container image.
    if tags and ("repository" in value or isinstance(image, str)):
        if not all(pinned_tag(tag) for tag in tags):
            raise Invalid(f"{label}: image tag/imageTag must be an explicit non-latest tag")
    elif isinstance(image, str) and not pinned_image(image):
        raise Invalid(f"{label}: image must have an explicit non-latest tag or sha256 digest")
    if isinstance(value.get("repository"), str) and not tags:
        digest = value.get("digest", "")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
            raise Invalid(f"{label}: image repository override requires a tag or sha256 digest")
    if isinstance(image, dict) and any(key in image for key in ("repository", "tag", "imageTag")):
        image_tags = [image[key] for key in ("tag", "imageTag") if key in image]
        digest = image.get("digest", "")
        if image_tags:
            if not all(pinned_tag(tag) for tag in image_tags):
                raise Invalid(f"{label}: image tag/imageTag must be an explicit non-latest tag")
        elif not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
            raise Invalid(f"{label}: image repository override requires a tag or sha256 digest")


def application_paths(resource, root, label, origin):
    workload = len(origin.parts) >= 2 and origin.parts[0] == "clusters" and (
        len(origin.parts) >= 3 or (root / origin).is_dir())
    home = bool(origin.parts) and origin.parts[0] in {"apps", "bootstrap"}
    if not (workload or home) or "management-cluster" in origin.parts:
        raise Invalid(f"{label}: Application is not allowed outside home/cluster Application trees")
    spec = resource.get("spec", {})
    sources = list(spec.get("sources") or [])
    if spec.get("source"):
        sources.append(spec["source"])
    for source in sources:
        helm = source.get("helm") or {}
        if isinstance(helm.get("values"), str):
            for values in parse(helm["values"], f"{label}:helm.values"):
                inspect(values, root, f"{label}:helm.values", [], origin)
        if "path" not in source:
            continue  # Helm chart sources and values-only refs have no local path.
        if not isinstance(source.get("repoURL"), str) or not SELF_REPOSITORY.fullmatch(source["repoURL"]):
            raise Invalid(f"{label}: Git source path requires this repository's repoURL")
        if source.get("targetRevision") != "main":
            raise Invalid(f"{label}: Git source path requires targetRevision: main")
        path = source["path"]
        if not isinstance(path, str) or not path:
            raise Invalid(f"{label}: Application path must be nonempty")
        resolved = (root / path).resolve()
        if Path(path).is_absolute() or not resolved.is_relative_to(root):
            raise Invalid(f"{label}: Application path escapes repository")
        if "management-cluster" in Path(path).parts or "management-cluster" in resolved.parts:
            raise Invalid(f"{label}: Application must not reference management-cluster/")
        if not resolved.is_dir():
            raise Invalid(f"{label}: Application path is not an existing directory: {path}")
        # Also prevent pointing at an ancestor that could sweep management RBAC in.
        if any(p.is_dir() for p in resolved.rglob("management-cluster")):
            raise Invalid(f"{label}: Application path contains management-cluster/")
        target = resolved.relative_to(root)
        if workload:
            allowed = (Path(*origin.parts[:2]), *SHARED_PATHS)
            if not any(target.is_relative_to(prefix) for prefix in allowed):
                raise Invalid(f"{label}: Application path crosses cluster boundary: {path}")
        elif home:
            if target == Path(".") or target.is_relative_to("clusters"):
                raise Invalid(f"{label}: home Application path crosses cluster boundary: {path}")


def inspect_document(value, root, label, resources, origin, ancestors=None, require_identity=False):
    if require_identity or isinstance(value, dict) and ("kind" in value or "apiVersion" in value):
        if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or not isinstance(value.get("apiVersion"), str):
            raise Invalid(f"{label}: resource must have both apiVersion and kind")
    inspect(value, root, label, resources, origin, ancestors)


def inspect(value, root, label, resources, origin, ancestors=None):
    ancestors = set() if ancestors is None else ancestors
    if not isinstance(value, (dict, list)):
        return
    if id(value) in ancestors:
        raise Invalid(f"{label}: recursive YAML alias")
    ancestors = ancestors | {id(value)}
    if isinstance(value, list):
        for item in value:
            inspect(item, root, label, resources, origin, ancestors)
        return
    kind = value.get("kind")
    api = value.get("apiVersion")
    if kind == "Secret" and ("data" in value or "stringData" in value):
        raise Invalid(f"{label}: Secret must not contain data or stringData")
    if kind == "Application" and value.get("apiVersion", "").startswith("argoproj.io/"):
        application_paths(value, root, label, origin)
    image_fields(value, label)
    is_kustomize = origin.name in KUSTOMIZATIONS and (api, kind) in (
        ("kustomize.config.k8s.io/v1beta1", "Kustomization"),
        ("kustomize.config.k8s.io/v1alpha1", "Component"),
    )
    if isinstance(kind, str) and isinstance(api, str) and not is_kustomize and (api, kind) != ("v1", "List"):
        resources.append(value)
    if (api, kind) == ("v1", "List"):
        if not isinstance(value.get("items"), list):
            raise Invalid(f"{label}: List.items must be an array of resources")
        for item in value["items"]:
            inspect_document(item, root, label, resources, origin, ancestors, require_identity=True)
    for key, child in value.items():
        if (api, kind) != ("v1", "List") or key != "items":
            inspect(child, root, label, resources, origin, ancestors)
    if kind == "ConfigMap":
        for name, content in (value.get("data") or {}).items():
            if isinstance(content, str) and name.endswith((".yaml", ".yml", ".json")):
                for embedded in parse(content, f"{label}:{name}"):
                    inspect_document(embedded, root, f"{label}:{name}", resources, origin)


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
            inspect_document(document, root, label, resources, path.relative_to(root))
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
            inspect_document(document, root, label, resources, directory.relative_to(root))
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
