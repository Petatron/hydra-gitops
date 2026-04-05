#!/usr/bin/env bash
# One-time bootstrap: installs ArgoCD, then hands control to the root app-of-apps.
# After this, all changes are driven by git commits to this repo.
set -euo pipefail

echo "=== 1/3: Install ArgoCD ==="
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml \
  --server-side --force-conflicts

echo "=== 2/3: Wait for ArgoCD to be ready ==="
kubectl -n argocd rollout status deployment argocd-server --timeout=180s

echo "=== 3/3: Apply root app-of-apps ==="
kubectl apply -f "$(dirname "$0")/root-app.yaml"

echo ""
echo "ArgoCD is running. Get the initial admin password:"
echo "  kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
echo ""
echo "Access the UI:"
echo "  kubectl -n argocd port-forward svc/argocd-server 8080:443"
echo "  open https://localhost:8080  (user: admin)"
