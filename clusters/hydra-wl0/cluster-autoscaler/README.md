# Cluster Autoscaler on hydra-wl0

Cluster Autoscaler needs **two** clusters. The Pods and Nodes it reacts to are
here on `hydra-wl0`; the `MachineDeployment`s it scales live on the management
cluster, alongside Cluster API and the Hydra provider. It runs here, with
in-cluster config for this side and a kubeconfig for the other.

Argo CD syncs everything in this directory. Two things it cannot:

| | where | why |
|---|---|---|
| management RBAC | `../management-cluster/` | targets a different cluster, which has no Argo CD |
| management kubeconfig Secret | created by hand | it is a credential, and this repo is public |

> Every command below names its cluster with `--context`. `kubectl --context X`
> applies to that one invocation only — it does **not** change your current
> context — so a later unqualified command silently hits whichever cluster your
> kubeconfig happens to be pointing at. Two of these steps target the
> management cluster and two target this one.

## What the management credential can actually do

Worth reading before you create it, because it is not as narrow as "scoped, not
admin" might suggest:

- **Reads are cluster-wide** and cannot be narrowed. Discovery lists and watches
  Cluster API objects across namespaces, and RBAC's `resourceNames` does not
  apply to `list`/`watch`. This token can read every Cluster API object on the
  management cluster, including other clusters'.
- **Writes are pinned** to `machinedeployments/scale` on `hydra-wl0-md-0` via
  `resourceNames`. It cannot scale `hydra-md-0` — which matters, because that
  pool is in the same `default` namespace and belongs to the management cluster
  itself.
- **The discovery flag is not a boundary.**
  `--node-group-auto-discovery=...,clusterName=hydra-wl0` only filters what the
  CA process chooses to act on. Anyone holding the token can ignore it; the
  `resourceNames` rule is the part that constrains a holder.

A holder can scale `hydra-wl0-md-0` or `pool-compute` to zero, causing Cluster
API to delete that pool's worker VMs. The autoscaler's own policy — `min-size`,
and the md-0 scale-down exemption — governs what *CA* chooses to do, not what a
token holder can request. The credential can also `update` Machines in
`default` (PET-13 scale-down), but a ValidatingAdmissionPolicy limits that to
adding or removing the `cluster.x-k8s.io/delete-machine` annotation on
`hydra-wl0`'s non-control-plane Machines. It cannot scale `hydra-md-0`, and it
cannot touch the management cluster's own Machines.

## Setup

### 1. Management cluster: identity for this autoscaler

```sh
kubectl --context <management> apply -f ../management-cluster/cluster-autoscaler-rbac.yaml
```

This also binds `cluster-api-provider-hydra-hydramachinetemplate-autoscaler-role`,
which the Hydra provider ships unbound (PET-27). That binding is what lets CA
size a pool sitting at zero replicas, where there is no Node left to inspect.

### 2. Build a kubeconfig from that ServiceAccount's token

Run against the **management** cluster. `$SERVER` must be an address reachable
from *this* cluster's pods, not from your laptop.

The `wait` is not optional. A `kubernetes.io/service-account-token` Secret is
populated asynchronously by the token controller, and `kubectl apply` in step 1
returns before `.data.token` exists. Read it too early and you get an empty
token, a kubeconfig that looks fine, and a CA that fails with 401 much later:

```sh
CTX=<management>
NS=cluster-autoscaler-hydra-wl0
SERVER=https://192.168.16.10:6443

kubectl --context "$CTX" -n "$NS" wait --for=jsonpath='{.data.token}' \
  --timeout=60s secret/cluster-autoscaler-hydra-wl0-token

TOKEN=$(kubectl --context "$CTX" -n "$NS" get secret cluster-autoscaler-hydra-wl0-token \
  -o jsonpath='{.data.token}' | base64 -d)
kubectl --context "$CTX" -n "$NS" get secret cluster-autoscaler-hydra-wl0-token \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/capi-ca.crt

[ -n "$TOKEN" ] || { echo "token is empty; do not continue"; exit 1; }

kubectl config --kubeconfig=/tmp/capi-kubeconfig set-cluster management \
  --server="$SERVER" --certificate-authority=/tmp/capi-ca.crt --embed-certs=true
kubectl config --kubeconfig=/tmp/capi-kubeconfig set-credentials ca --token="$TOKEN"
kubectl config --kubeconfig=/tmp/capi-kubeconfig set-context management \
  --cluster=management --user=ca
kubectl config --kubeconfig=/tmp/capi-kubeconfig use-context management
```

Confirm it works, and that it is as constrained as claimed, before installing it:

```sh
kubectl --kubeconfig=/tmp/capi-kubeconfig -n default get machinedeployments
kubectl --kubeconfig=/tmp/capi-kubeconfig auth can-i patch machinedeployments/hydra-wl0-md-0 \
  --subresource=scale -n default                  # expect: yes
kubectl --kubeconfig=/tmp/capi-kubeconfig auth can-i patch machinedeployments/hydra-md-0 \
  --subresource=scale -n default                  # expect: no
```

### 3. Install it here

The namespace has to exist first. Argo creates it via `CreateNamespace=true`,
but only when it syncs this Application — so if you are doing this before the
PR merges, create it yourself. Both orders work; the Deployment simply sits in
`CreateContainerConfigError` until the Secret exists.

```sh
kubectl --context hydra-wl0 create namespace cluster-autoscaler \
  --dry-run=client -o yaml | kubectl --context hydra-wl0 apply -f -

kubectl --context hydra-wl0 -n cluster-autoscaler \
  create secret generic cluster-autoscaler-management-kubeconfig \
  --from-file=kubeconfig=/tmp/capi-kubeconfig \
  --dry-run=client -o yaml | kubectl --context hydra-wl0 apply -f -

shred -u /tmp/capi-kubeconfig /tmp/capi-ca.crt
```

Both are piped through `apply` rather than run as bare `create`, which is not
re-runnable — re-issuing the token later should not require deleting the Secret
by hand first.

The Secret is invisible to Argo CD — `prune: true` only removes resources Argo
already tracks, and this one carries none of its metadata — so it survives
syncs. It also means Argo will not recreate it: if it is lost, redo this step.

### 4. Node groups have to opt in

A MachineDeployment is not a node group until it says what its bounds are. On
the **management** cluster:

```sh
kubectl --context <management> -n default annotate machinedeployment hydra-wl0-md-0 \
  cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size=1 \
  cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size=3 \
  cluster.x-k8s.io/autoscaling-options-scaledownutilizationthreshold=0
```

**The third annotation exempts this pool from scale-down (PET-13).** One of its
two workers runs all of Argo CD and nothing in this cluster has a
PodDisruptionBudget, so it must not be drained just because it looks idle. A node
counts as underutilised only when utilisation is **at or below** the threshold,
so `"0"` protects every node that requests anything — and each md-0 node's
DaemonSets alone request 20m CPU. It would *not* protect a node with literally
nothing requested. The key is lowercase and the prefix match is case-sensitive.
A value that does not parse logs `failed to convert autoscaling_options option
… to float` at warning level and reverts to the 0.5 default; an **empty** value
is skipped with no log at all. Either way the pool loses its exemption, so check
it after any edit — see [Verifying](#verifying).

The two bounds do not gate discovery symmetrically, which is easy to get wrong.
A missing annotation reads as `0`; then `max < min` is a hard error — one that
aborts discovery for **every** pool, not just this one — and `max == 0` is a
silent skip. So a missing `min-size` gives a discovered pool with a floor of
zero, not an ignored one. The full rule, and how to choose the bounds, is
`cluster-api-provider-hydra/docs/autoscaling-policy.md` (PET-11).

**A pool CA should scale also has to be named in the RBAC.** The write role in
`../management-cluster/cluster-autoscaler-rbac.yaml` pins
`machinedeployments/scale` to a `resourceNames` list, so a new pool is discovered,
bounded and even chosen for scale-up — and then refused with `forbidden ... cannot
patch resource "machinedeployments/scale"`. Nothing in the pool's own status says
so. PET-12 hit exactly this for `pool-compute`.

## Verifying

```sh
kubectl --context hydra-wl0 -n cluster-autoscaler logs deploy/cluster-autoscaler \
  | grep -iE "node ?group|clusterapi|permission|forbidden|infrastructure reference|unremovable|failed to convert autoscaling_options"
```

The optional space in `node ?group` is deliberate: CA logs `discovered node group:
%s` but `nodegroup %s has no scaling capacity, skipping` — and the second is the
one that explains a missing pool.

**The md-0 exemption is working** when every md-0 node reports, at `-v=4`:

```
Node hydra-wl0-md-0-... unremovable: cpu requested (6.02% of allocatable) is above the scale-down utilization threshold
```

The resource named is whichever of cpu and memory has the higher request
fraction — ties go to memory — so a `memory requested` line is the same success,
not a failed exemption. (A GPU node reports its GPU resource instead.)

With the default threshold those same nodes would be *candidates* instead. If
you see an md-0 node listed as unneeded, or a `failed to convert
autoscaling_options` warning, the exemption is not in force.

**Do not apply the Machine `update` grant until both md-0 nodes show that line.**
The order is what makes the window after Argo syncs safe: without the Role, CA's
`MarkMachineForDeletion` is refused with a 403, and `DeleteNodes` returns on a
failed mark *before* it calls `SetSize` — verified in the 1.35.2 source — so no
replica count changes. Once the Role exists, the exemption annotation is the only
thing standing between an idle md-0 node and a drain: the admission policy
admits marks on md-0 workers, because they are non-control-plane `hydra-wl0`
Machines.

### Exercising the scale-down admission policy

Scale-down's `update` on Machines cannot be pinned by name, so it is fenced by a
ValidatingAdmissionPolicy (see the RBAC file). Run this after every apply of that
file. It makes no changes — every update is `--dry-run=server`, which still goes
through admission. One must be admitted and four refused:

```sh
SA=system:serviceaccount:cluster-autoscaler-hydra-wl0:cluster-autoscaler-hydra-wl0
M="kubectl --context <management> -n default"
try() { $M get machine "$1" -o json | jq "$2" | $M replace --dry-run=server --as="$SA" -f - 2>&1 | tail -1; }

WORKER=$($M get machines -l 'cluster.x-k8s.io/cluster-name=hydra-wl0,!cluster.x-k8s.io/control-plane' -o name | head -1 | cut -d/ -f2)
CP=$($M get machines -l 'cluster.x-k8s.io/cluster-name=hydra-wl0,cluster.x-k8s.io/control-plane' -o name | head -1 | cut -d/ -f2)
OTHER=$($M get machines -l 'cluster.x-k8s.io/cluster-name!=hydra-wl0' -o name | head -1 | cut -d/ -f2)

try "$WORKER" '.metadata.annotations["cluster.x-k8s.io/delete-machine"]="dry-run"'  # ADMITTED
try "$OTHER"  '.metadata.annotations["cluster.x-k8s.io/delete-machine"]="dry-run"'  # refused: another cluster
try "$CP"     '.metadata.annotations["cluster.x-k8s.io/delete-machine"]="dry-run"'  # refused: control plane
try "$WORKER" '.metadata.annotations["example.com/probe"]="1"'                      # refused: other annotation
try "$WORKER" '.metadata.labels["example.com/probe"]="1"'                           # refused: label
```

Use `replace`, not `annotate`: `kubectl annotate` sends a PATCH, which RBAC
checks as the `patch` verb — not granted — so it would be refused before the
policy is ever consulted, and prove nothing about the policy.

A healthy start names the discovered group and its bounds. Things to know before
reading the output:

- **Discovery failures are quiet.** Missing annotations, missing RBAC and an
  unresolvable `infrastructureRef` all end with CA skipping the node group and
  logging at `-v=4`. A pool that will not scale looks identical to one CA never
  saw, which is why this Deployment runs with `--v=4`.
- **A permissions failure on the kubeconfig looks nothing like a discovery
  problem.** The image runs as uid 65532, and Secret volume files are owned by
  root, so the pod sets `fsGroup: 65532` with mode `0440`. Without that the
  process cannot open `/etc/capi/kubeconfig` and the pod crash-loops before it
  discovers anything.
- **Scale-down is on (PET-13), and exempt for `hydra-wl0-md-0`.** Only pools
  without the exemption can shrink; today that is `pool-compute`. Removing the
  exemption makes md-0's workers — one of which runs all of Argo CD, with no
  PodDisruptionBudget — candidates the moment they look idle.
- **The management connection crosses sites.** The management control plane is
  at a different site from these VMs — measured 50 ms from a pod here. A
  partition between them stops autoscaling: CA can neither read
  MachineDeployments nor write replica counts. That is a property of the
  reference lab, not of Hydra, but it makes timings here unrepresentative.
