# hydra-gitops

Argo CD configuration for two Kubernetes clusters: the hand-built **home
cluster** and the Hydra-created **hydra-wl0** workload cluster. Each root
Application watches `main` and reconciles its own tree.

> **A merge to `main` changes live clusters automatically**, normally within
> about three minutes. Applications self-heal drift and generally prune removed
> resources. The hydra-wl0 Cilium Application deliberately disables pruning and
> omits the deletion finalizer so losing its Application does not delete the CNI.
> Review cluster targeting and deletion impact before merging.

> **Never apply top-level `apps/` or `bootstrap/root-app.yaml` to hydra-wl0.**
> The home Cilium configuration defaults to pod CIDR `10.0.0.0/8`, while hydra-wl0
> uses `10.244.0.0/16`; it also enables kube-proxy replacement while hydra-wl0
> still runs kube-proxy. The home infrastructure tree includes a MetalLB pool
> (`192.168.15.200-250`) inside the site's DHCP range, risking address collisions.
> hydra-wl0 deliberately has no MetalLB Application. See the
> [cluster-specific explanation](clusters/hydra-wl0/README.md).

## Architecture

```text
main
├── bootstrap/root-app.yaml       → home cluster's Argo CD
│   └── apps/                    → Argo CD, Cilium, KEDA, MetalLB
│       └── infrastructure/      → namespaces, MetalLB pool, local-path storage
└── clusters/hydra-wl0/root-app.yaml → hydra-wl0's Argo CD
    └── clusters/hydra-wl0/apps/   → Cilium, storage, Cluster Autoscaler
        ├── infrastructure/storage/              (shared)
        └── clusters/hydra-wl0/cluster-autoscaler/

Cluster Autoscaler in hydra-wl0
├── in-cluster identity           → workload Pods and Nodes
└── manually supplied kubeconfig → Cluster API on the management cluster
                                   (management RBAC is applied separately)
```

The autoscaler's **management cluster** is a separate target from hydra-wl0.
It has no Argo CD managing the `management-cluster/` manifests; they must never
be wired into an Application. The two GitOps roots above describe the repo's
configuration, not a claim that both roots are currently installed or healthy.

## Repository layout

```text
.github/                           # Validation workflow, PR template, CODEOWNERS
scripts/ci/                        # Local/CI validator and failure-case tests
schemas/                           # Pinned Hydra CRD JSON schemas and provenance
bootstrap/                         # Home cluster's one-time entry point
├── install.sh                     # Installs Argo CD and applies the home root
├── argo-cd.yaml                    # Bootstrap-era Argo CD Application reference
└── root-app.yaml                  # Watches only apps/
apps/                              # Home cluster Applications
├── argo-cd.yaml
├── cilium.yaml
├── keda.yaml
├── metallb.yaml
└── infrastructure.yaml            # Watches infrastructure/ recursively
infrastructure/
├── metallb/address-pool.yaml      # Home pool; unsafe on hydra-wl0's DHCP network
├── namespaces/namespaces.yaml
└── storage/local-path.yaml        # Shared local-path provisioner and default SC
clusters/hydra-wl0/
├── README.md
├── root-app.yaml                  # Watches only clusters/hydra-wl0/apps/
├── apps/                          # Cilium, shared storage, autoscaler Applications
├── cluster-autoscaler/            # Workload Deployment, RBAC, setup runbook
└── management-cluster/            # Apply manually to management; never via Argo
```

## Bootstrap and manual prerequisites

Run commands from this repository's root. Confirm the target with
`kubectl config current-context` before any bootstrap operation.

**Home cluster only:** `bash bootstrap/install.sh` installs Argo CD using the
current kubectl context, waits for it, then applies `bootstrap/root-app.yaml`.
This script is not the hydra-wl0 bootstrap procedure.

**hydra-wl0:** follow the
[cluster bootstrap and GitOps handoff runbook](https://github.com/Petatron/hydra-bootstrap/blob/main/docs/cluster-bootstrap.md).
The kubeadm cluster and Cilium must already work before Argo CD can run. The
cluster-specific root adopts the existing Cilium settings; it is not a CNI
bootstrap mechanism. With that prerequisite met:

```sh
kubectl --context hydra-wl0 create namespace argocd --dry-run=client -o yaml \
  | kubectl --context hydra-wl0 apply -f -
kubectl --context hydra-wl0 apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml \
  --server-side
kubectl --context hydra-wl0 -n argocd rollout status deployment/argocd-server --timeout=180s
kubectl --context hydra-wl0 apply -f clusters/hydra-wl0/root-app.yaml
```

Both bootstrap paths currently use Argo CD's floating `stable` install URL.
Bootstrap/steady-state ownership, sync ordering, chart pinning, and layout
restructuring are tracked in [PET-18](https://linear.app/petatron/issue/PET-18).

**Autoscaler prerequisites remain outside Argo CD.** Follow the ordered commands
and credential checks in the [autoscaler runbook](clusters/hydra-wl0/cluster-autoscaler/README.md):

1. On the management cluster, apply
   `clusters/hydra-wl0/management-cluster/cluster-autoscaler-rbac.yaml`. The Hydra
   provider must already supply the referenced autoscaler ClusterRole.
2. Wait for the ServiceAccount token, build and verify a management kubeconfig
   whose server is reachable from hydra-wl0's pods, and create the
   `cluster-autoscaler-management-kubeconfig` Secret in hydra-wl0's
   `cluster-autoscaler` namespace. Create that namespace first if Argo has not
   synced yet. Keep credentials out of Git and remove temporary credential files.
3. On the management cluster, set both autoscaler min/max annotations on the
   intended MachineDeployment. Without them, CA discovers no node group.

Repeat credential setup after loss or rotation; Argo CD does not recreate that
untracked Secret. Verify actual node-group discovery in autoscaler logs, since
pod readiness alone does not establish that autoscaling works. Existing
scale-down policy remains disabled pending PET-13.

For the Argo CD UI, substitute the intended cluster context:

```sh
kubectl --context <cluster> -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
kubectl --context <cluster> -n argocd port-forward svc/argocd-server 8080:443
# https://localhost:8080, user admin
```

## Making a change

Create a branch and PR. Add or edit an Application under the intended cluster's
`apps/` tree, use an explicit chart version for new Applications, and edit
`helm.valuesObject` for chart settings. Git-backed source paths must exist in
this repository on `targetRevision: main` and cannot include management-cluster
manifests. Applications under `clusters/<name>/` may reference only their own
cluster tree or the shared `infrastructure/storage/` subtree. Home Applications
under `apps/` and `bootstrap/` cannot reference `clusters/`. Resolved paths enforce
these boundaries through `..`, including Kustomize output. Repository symlinks
are rejected, including in-repository, extensionless, directory, and dangling
links: watched paths must not alias manual-only management resources.
Applications are forbidden in `infrastructure/` (including shared storage),
`management-cluster/`, and other unclassified paths. These trees contain leaf
resources, not additional reconciliation roots; the rule also covers Helm-only
and generated Applications.
Use explicit
image tags (never `latest`) or SHA-256 digests, including embedded helper Pods.

The [PR template](.github/pull_request_template.md) records the target cluster,
manual prerequisites, verification, and rollback. Merge only after validation
and review. Argo CD then reconciles `main`; manual edits to tracked resources
may be reverted by self-healing. Roll back Git-managed configuration through a
revert PR, and handle any manual prerequisites in the named cluster separately.

## Validation and merge gates

[Validate GitOps](.github/workflows/validate.yaml) runs on **every PR**, pushes to
`main`, merge queues, and manual dispatches. The stable required check name is
**GitOps validation**. No path filter can leave a documentation PR waiting for a
check that never starts. The workflow has read-only repository permissions and
needs no cluster credentials.

It parses repository YAML/JSON with duplicate-key rejection (YAML merge keys and
explicit inherited-key overrides are supported; date-like scalars stay strings); validates Kubernetes
resources against **1.35.3** schemas and pinned CRD schemas; verifies Application
paths, repository identity, branch, and cluster boundaries (including multi-source
Applications); rejects management-cluster paths,
Secret payloads, and tagless/`latest` images; and runs `kustomize build` for every
`kustomization.yaml`, `kustomization.yml`, or `Kustomization`. Rendered output and
YAML/JSON embedded in ConfigMaps receive the same policy and schema checks.
Schema validation collects document roots and `List.items`, not nested object
references such as an HPA `scaleTargetRef`; the parent schema validates those fields.
Unknown resource schemas fail validation. Empty ServiceAccount token Secret
manifests are allowed because the API server fills their data outside Git.
Here, "empty" means both `data` and `stringData` are omitted; even empty maps
under those keys are rejected by the repository policy. Every `v1/List` item
must have its own `apiVersion` and `kind`. Schema exemptions cover only supported
Kustomization/Component configs in recognized build-entrypoint filenames.

Run exactly the same validation locally (Python 3.9+; CI uses 3.12):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/ci/requirements.txt
bash scripts/ci/install-tools.sh "$PWD/.venv/bin"
export PATH="$PWD/.venv/bin:$PATH"
actionlint
python -m unittest discover -s scripts/ci -p 'test_*.py' -v
python scripts/ci/validate.py
```

Tests create temporary repositories and prove each required rule fails on broken
input, including generated Secrets and images changed by Kustomize. Tool and
schema downloads require internet access. Release archives are checked against
[committed SHA-256 hashes](scripts/ci/tool-checksums.txt), not newly downloaded
checksums. When updating tool versions, review the upstream release checksums
and update the installer and hashes together for Linux/macOS amd64/arm64.
See [schema provenance and update
instructions](schemas/README.md).

Image checks cover container `image` strings and common explicit Helm overrides
in `valuesObject` and inline `values`: `image: {repository, tag/digest}`, tag-only
`image` maps, and sibling `image`/`repository` plus `tag`/`imageTag`. Explicit
repository overrides within those Helm values need a tag or SHA-256 digest; empty and `latest` tags fail.
SHA-256 image digests require exactly 64 lowercase hexadecimal characters,
including explicit Helm digests supplied alongside a tag.
Ordinary `repository`, `tag`, and `digest` fields outside Helm values are not image
overrides. Structured Hydra VM images are not treated as container images.

CI validates checked-in manifests and Kustomize output. It does not render the
external Helm charts, evaluate Kubernetes CEL/admission rules, or prove runtime
behaviour such as RBAC sufficiency or network reachability. Omitted chart defaults,
chart-specific image keys, Helm parameters, and external values files are not
evaluated by these image heuristics. Cluster API kinds
named in RBAC rules are strings, not custom resources to schema-validate.

Repository settings on `main` require **GitOps validation** from GitHub Actions,
an up-to-date branch, **one approving review**, and **resolved conversations**;
force pushes and branch deletion remain disabled. `CODEOWNERS` assigns
`@Eyasluna` for review on other authors' PRs. GitHub does not let authors approve
their own PRs. **Admin enforcement remains off deliberately** for the single
active maintainer's recovery/self-merge path: administrators can bypass these
gates and should normally follow the same validation/review process. Branch
protection lives in GitHub settings and must be configured separately if this
repository is forked or recreated.

## Related repositories

- [hydra-infra](https://github.com/Petatron/hydra-infra) — Terraform and existing-node lifecycle
- [hydra-bootstrap](https://github.com/Petatron/hydra-bootstrap) — cluster bootstrap and handoff runbooks
- [cluster-api-provider-hydra](https://github.com/Petatron/cluster-api-provider-hydra) — Cluster API infrastructure provider
