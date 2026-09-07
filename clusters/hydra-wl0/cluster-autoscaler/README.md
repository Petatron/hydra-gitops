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

It cannot destroy VMs the way cluster-admin could, and it holds no `update` on
`machines` at all until PET-13 adds scale-down.

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
kubectl --kubeconfig=/tmp/capi-kubeconfig auth can-i patch machinedeployments \
  --subresource=scale -n default hydra-wl0-md-0     # expect: yes
kubectl --kubeconfig=/tmp/capi-kubeconfig auth can-i patch machinedeployments \
  --subresource=scale -n default hydra-md-0         # expect: no
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
  cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size=3
```

Without both annotations CA discovers nothing, and says so only at `-v=4`. The
bounds above are PET-10's minimum to demonstrate discovery; the actual policy —
and whether `min` should be `0` — is PET-11.

## Verifying

```sh
kubectl --context hydra-wl0 -n cluster-autoscaler logs deploy/cluster-autoscaler \
  | grep -iE "node group|clusterapi|permission|forbidden"
```

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
- **`--scale-down-enabled=false`** here. PET-10 proves discovery; scale-down is
  PET-13. Enabling it before then would let CA drain this cluster's workers the
  moment it judged them underutilised.
- **The management connection crosses sites.** The management control plane is
  at a different site from these VMs — measured 50 ms from a pod here. A
  partition between them stops autoscaling: CA can neither read
  MachineDeployments nor write replica counts. That is a property of the
  reference lab, not of Hydra, but it makes timings here unrepresentative.
