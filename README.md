# hydra-gitops

GitOps repo for the home K8s cluster. ArgoCD watches this repo and reconciles all add-ons and workloads automatically.

## Architecture

```
                    ┌──────────────────────────────────┐
                    │  this repo (hydra-gitops)         │
                    │                                   │
                    │  apps/              infrastructure/
                    │  ├─ argo-cd.yaml     ├─ metallb/
                    │  ├─ cilium.yaml      ├─ namespaces/
                    │  ├─ keda.yaml        └─ storage/
                    │  ├─ metallb.yaml
                    │  └─ infrastructure.yaml
                    └──────────┬───────────────────────┘
                               │ ArgoCD watches main branch
                               ▼
                    ┌──────────────────────┐
                    │  ArgoCD (root app)   │
                    │  app-of-apps pattern │
                    └──┬───┬───┬───┬──────┘
                       │   │   │   │  auto-sync
                       ▼   ▼   ▼   ▼
                    ArgoCD Cilium KEDA MetalLB infra manifests
```

**To add an add-on**: create a new `apps/<name>.yaml` ArgoCD Application, push to `main`.
**To upgrade a chart**: bump `targetRevision` in the app YAML, push to `main`.
**To change values**: edit `helm.valuesObject` in the app YAML, push to `main`.

## First-Time Bootstrap

```bash
# 1. Install ArgoCD + apply the root app-of-apps
./bootstrap/install.sh

# 2. Get the admin password
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo

# 3. Access the ArgoCD UI
kubectl -n argocd port-forward svc/argocd-server 8080:443
# open https://localhost:8080  (user: admin)
```

After bootstrap, **all management is done via git**. Push changes to `main` and ArgoCD applies them.

## Repo Layout

```
├── bootstrap/                       # One-time setup (run manually once)
│   ├── install.sh                   # Installs ArgoCD + root app
│   ├── argo-cd.yaml                 # ArgoCD Helm values reference
│   └── root-app.yaml               # Root app-of-apps (watches apps/)
├── apps/                            # ArgoCD Application manifests (one per add-on)
│   ├── argo-cd.yaml                 # ArgoCD self-management (Helm)
│   ├── cilium.yaml                  # Cilium CNI + Hubble UI
│   ├── keda.yaml                    # KEDA autoscaler
│   ├── metallb.yaml                 # MetalLB load balancer
│   └── infrastructure.yaml          # Points to infrastructure/ dir
└── infrastructure/                  # Raw K8s manifests (namespaces, storage, etc.)
    ├── metallb/
    │   └── address-pool.yaml        # IPAddressPool 192.168.15.200-250 + L2
    ├── namespaces/
    └── storage/
        └── local-path.yaml         # Rancher local-path-provisioner (default SC)
```

## Adding a New App

Create `apps/<name>.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: <name>
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://charts.example.com
    chart: <chart-name>
    targetRevision: "1.*"
    helm:
      valuesObject:
        key: value
  destination:
    server: https://kubernetes.default.svc
    namespace: <target-namespace>
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

Push to `main`. ArgoCD picks it up within ~3 minutes (or click "Refresh" in the UI).

## Related

- **[hydra-infra](../hydra-infra)** — Terraform IaC for KVM worker VMs and node lifecycle
