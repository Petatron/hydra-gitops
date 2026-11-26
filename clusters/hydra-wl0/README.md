# hydra-wl0

The first cluster Hydra built from nothing (PET-16 / PET-17): three kube-vip
fronted control-plane nodes and two workers, all VMs created through
`cluster-api-provider-hydra`.

## Why this cluster has its own path

The top-level `apps/` tree was written for the hand-built home cluster, and
applying it here would break this one. Two concrete conflicts, both found by
comparing it against what is actually running:

| | top-level `apps/` | running on hydra-wl0 |
|---|---|---|
| Cilium pod CIDR | unset, so the chart default `10.0.0.0/8` | `10.244.0.0/16` |
| `kubeProxyReplacement` | `true` | `false` — kube-proxy is installed |

Syncing that Cilium app would renumber every pod and enable kube-proxy
replacement alongside a running kube-proxy.

`infrastructure/metallb/address-pool.yaml` is a third problem, and a worse one:
it hands MetalLB `192.168.15.200-250`, which is **inside the site's DHCP range**.
That router has been observed leasing `.200`, `.234` and `.243`. MetalLB would
ARP for addresses the DHCP server may hand to a machine at the same time.
MetalLB is deliberately not part of this cluster.

Sharing what is genuinely shareable is still the goal — `apps/storage.yaml` here
points straight at the top-level `infrastructure/storage`, because that manifest
has nothing cluster-specific in it. Restructuring so the split is by *what varies*
rather than by copy-paste is PET-18.

## Cilium is hydra's, not Argo CD's

Since PET-101, hydra-wl0's Cilium is installed and kept in step by Cluster API Add-on Provider Helm
on the management cluster. The input is `cni: hydra` and `ciliumVersion` in [`hydra.yaml`](hydra.yaml),
applied with the `hydra` CLI. Argo CD used to own it through `apps/cilium.yaml`; that Application had
no finalizer, so deleting it left Cilium running for CAAPH to keep.

- **Change Cilium** (version, MTU) in `hydra.yaml`, then `hydra cluster plan` and `hydra cluster apply`.
- **Never add an Argo CD Application for it** here, and never remove the Cluster's
  `cni.hydra.io/cilium` label: that uninstalls Cilium from the running cluster.
- **`kube-system/cilium-ca` is not rendered by the chart** (Hubble certificates come from certgen). It
  carries `helm.sh/resource-policy: keep`; do not delete it, or certgen mints a new CA.

## Bootstrapping

```
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml --server-side
kubectl apply -f clusters/hydra-wl0/root-app.yaml
```

These commands assume the current context is hydra-wl0 and Argo CD's namespace
already exists; see the [root bootstrap instructions](../../README.md#bootstrap-and-manual-prerequisites)
for commands with explicit contexts and the Cilium prerequisite.

Argo CD then reconciles the Applications in this tree. Cluster Autoscaler still
requires [manual management RBAC, credential setup, and node-group annotations](cluster-autoscaler/README.md#setup).
