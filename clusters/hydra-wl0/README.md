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

## Bootstrapping

```
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml --server-side
kubectl apply -f clusters/hydra-wl0/root-app.yaml
```

Everything after that is driven by commits to this repo.
