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

## Setup

### 1. Management cluster: identity for this autoscaler

```sh
kubectl --context <management> apply -f ../management-cluster/cluster-autoscaler-rbac.yaml
```

This creates a ServiceAccount with a **narrow** role — read and scale
MachineDeployments, read HydraMachineTemplates — and nothing more. It is not
management admin, on purpose: its token ends up inside this cluster, so anyone
with access here gains exactly those permissions over there. Admin would mean
the workload cluster could destroy VMs belonging to any cluster the management
cluster runs.

It also binds `cluster-api-provider-hydra-hydramachinetemplate-autoscaler-role`,
which the Hydra provider ships unbound (PET-27). That binding is what lets CA
size a pool sitting at zero replicas, where there is no Node left to inspect.

### 2. Build a kubeconfig from that ServiceAccount's token

Run against the **management** cluster. `$SERVER` must be an address reachable
from *this* cluster's pods, not from your laptop:

```sh
NS=cluster-autoscaler-hydra-wl0
SERVER=https://192.168.16.10:6443
TOKEN=$(kubectl -n $NS get secret cluster-autoscaler-hydra-wl0-token -o jsonpath='{.data.token}' | base64 -d)
kubectl -n $NS get secret cluster-autoscaler-hydra-wl0-token -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/capi-ca.crt

kubectl config --kubeconfig=/tmp/capi-kubeconfig set-cluster management \
  --server="$SERVER" --certificate-authority=/tmp/capi-ca.crt --embed-certs=true
kubectl config --kubeconfig=/tmp/capi-kubeconfig set-credentials ca --token="$TOKEN"
kubectl config --kubeconfig=/tmp/capi-kubeconfig set-context management \
  --cluster=management --user=ca
kubectl config --kubeconfig=/tmp/capi-kubeconfig use-context management
```

### 3. Install it here

```sh
kubectl --context hydra-wl0 -n cluster-autoscaler \
  create secret generic cluster-autoscaler-management-kubeconfig \
  --from-file=kubeconfig=/tmp/capi-kubeconfig

shred -u /tmp/capi-kubeconfig /tmp/capi-ca.crt
```

The Secret is invisible to Argo CD — `prune: true` only removes resources Argo
already tracks, and this one carries none of its metadata — so it survives
syncs. It also means Argo will not recreate it: if it is lost, redo this step.

### 4. Node groups have to opt in

A MachineDeployment is not a node group until it says what its bounds are. On
the **management** cluster:

```sh
kubectl -n default annotate machinedeployment hydra-wl0-md-0 \
  cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size=1 \
  cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size=3
```

Without both annotations CA discovers nothing, and says so only at `-v=4`. The
bounds above are PET-10's minimum to demonstrate discovery; the actual policy —
and whether `min` should be `0` — is PET-11.

## Verifying

```sh
kubectl -n cluster-autoscaler logs deploy/cluster-autoscaler | grep -iE "node group|clusterapi"
```

A healthy start names the discovered group and its bounds. Things to know before
reading the output:

- **Discovery failures are quiet.** Missing annotations, missing RBAC and an
  unresolvable `infrastructureRef` all end with CA skipping the node group and
  logging at `-v=4`. A pool that will not scale looks identical to one CA never
  saw, which is why this Deployment runs with `--v=4`.
- **`--scale-down-enabled=false`** here. PET-10 proves discovery; scale-down is
  PET-13. Enabling it before then would let CA drain this cluster's workers the
  moment it judged them underutilised.
- **The management connection crosses sites.** The management control plane is
  at a different site from these VMs — measured 50 ms from a pod here. A
  partition between them stops autoscaling: CA can neither read
  MachineDeployments nor write replica counts. That is a property of the
  reference lab, not of Hydra, but it makes timings here unrepresentative.
