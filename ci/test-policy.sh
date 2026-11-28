#!/usr/bin/env bash
# Tests the hydra-ci-guard admission policy and the CI identity's RBAC in
# ci/hydra-mgmt-access.yaml, in a throwaway kind cluster (needs kind, kubectl,
# Docker). Stand-in CRDs accept any spec; the manifest is applied as is, and
# each request is made as the CI identity, dry run and real.
#
#   bash ci/test-policy.sh
#
# Run it after any change to ci/hydra-mgmt-access.yaml. It deletes its
# cluster on exit.
set -u
NAME=hydra-ci-policy-test
ROOT=$(cd "$(dirname "$0")/.." && pwd)
kind create cluster --name "$NAME" --wait 120s >/dev/null || exit 1
trap 'kind delete cluster --name "$NAME" >/dev/null 2>&1' EXIT
kubectl --context "kind-$NAME" apply -f - >/dev/null <<'CRDS' || exit 1
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: clusters.cluster.x-k8s.io}
spec:
  group: cluster.x-k8s.io
  scope: Namespaced
  names: {plural: clusters, singular: cluster, kind: Cluster, listKind: ClusterList}
  versions:
  - name: v1beta2
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: machinedeployments.cluster.x-k8s.io}
spec:
  group: cluster.x-k8s.io
  scope: Namespaced
  names: {plural: machinedeployments, singular: machinedeployment, kind: MachineDeployment, listKind: MachineDeploymentList}
  versions:
  - name: v1beta2
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: kubeadmcontrolplanes.controlplane.cluster.x-k8s.io}
spec:
  group: controlplane.cluster.x-k8s.io
  scope: Namespaced
  names: {plural: kubeadmcontrolplanes, singular: kubeadmcontrolplane, kind: KubeadmControlPlane, listKind: KubeadmControlPlaneList}
  versions:
  - name: v1beta2
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io}
spec:
  group: bootstrap.cluster.x-k8s.io
  scope: Namespaced
  names: {plural: kubeadmconfigtemplates, singular: kubeadmconfigtemplate, kind: KubeadmConfigTemplate, listKind: KubeadmConfigTemplateList}
  versions:
  - name: v1beta2
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: hydramachinetemplates.infrastructure.cluster.x-k8s.io}
spec:
  group: infrastructure.cluster.x-k8s.io
  scope: Namespaced
  names: {plural: hydramachinetemplates, singular: hydramachinetemplate, kind: HydraMachineTemplate, listKind: HydraMachineTemplateList}
  versions:
  - name: v1beta1
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: helmchartproxies.addons.cluster.x-k8s.io}
spec:
  group: addons.cluster.x-k8s.io
  scope: Namespaced
  names: {plural: helmchartproxies, singular: helmchartproxy, kind: HelmChartProxy, listKind: HelmChartProxyList}
  versions:
  - name: v1alpha1
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: hydraclusters.infrastructure.cluster.x-k8s.io}
spec:
  group: infrastructure.cluster.x-k8s.io
  scope: Namespaced
  names: {plural: hydraclusters, singular: hydracluster, kind: HydraCluster, listKind: HydraClusterList}
  versions:
  - name: v1beta1
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}}
CRDS
kubectl --context "kind-$NAME" wait --for condition=established crd --all --timeout=60s >/dev/null || exit 1
kubectl --context "kind-$NAME" apply -f "$ROOT/ci/hydra-mgmt-access.yaml" >/dev/null || exit 1

K="kubectl --context kind-$NAME -n default"
SA="--as=system:serviceaccount:hydra-ci-access:hydra-ci"
pass=0; fail=0
obj() { # kind name labels-json spec-json
  printf '{"apiVersion":"%s","kind":"%s","metadata":{"name":"%s","namespace":"default","labels":%s},"spec":%s}' "$1" "$2" "$3" "$4" "$5"; }
check() { # expect desc -- command...
  local want=$1 desc=$2; shift 3
  out=$("$@" 2>&1); rc=$?
  got=allow; [ $rc -ne 0 ] && got=deny
  if [ "$got" = "$want" ]; then pass=$((pass+1)); echo "ok    $want  $desc"; else fail=$((fail+1)); echo "FAIL  want $want got $got  $desc :: $(echo "$out" | tail -1)"; fi
}
C=cluster.x-k8s.io/v1beta2; KCPV=controlplane.cluster.x-k8s.io/v1beta2; KCTV=bootstrap.cluster.x-k8s.io/v1beta2
HMTV=infrastructure.cluster.x-k8s.io/v1beta1; HCPV=addons.cluster.x-k8s.io/v1alpha1
mg='{"hydra.io/cluster":"hydra-mgmt"}'; wl='{"hydra.io/cluster":"hydra-wl0"}'
# Seed as admin: the objects that exist on hydra-mgmt today.
for o in "$(obj $C Cluster hydra-mgmt "$mg" '{}')" "$(obj $KCPV KubeadmControlPlane hydra-mgmt-cp "$mg" '{"replicas":3}')" \
         "$(obj $HMTV HydraMachineTemplate hydra-mgmt-cp-pdh '{}' '{}')" "$(obj $HMTV HydraMachineTemplate hydra-wl0-md-0 '{}' '{}')" \
         "$(obj $KCPV KubeadmControlPlane hydra-wl0-cp "$wl" '{"replicas":3}')" \
         "$(obj $HCPV HelmChartProxy foreign-proxy "$wl" '{"clusterSelector":{"matchLabels":{"cni.hydra.io/cilium":"hydra-mgmt"}}}')" \
         "$(obj $HCPV HelmChartProxy hydra-wl0-old "$wl" '{"clusterSelector":{"matchLabels":{"hydra.io/cluster":"hydra-wl0"}}}')"; do
  # A seed that failed would turn later "deny" cases into misses that pass.
  echo "$o" | $K apply -f - >/dev/null || { echo "seeding failed: $o" >&2; exit 1; }
done

echo "== hydra-mgmt: plan (dry run) passes, real writes are refused"
check allow "dry-run create Cluster hydra-mgmt (plan)" -- bash -c "echo '$(obj $C Cluster hydra-mgmt "$mg" '{"x":1}')' | $K $SA apply --server-side --field-manager=hydra-hydra-mgmt --force-conflicts --dry-run=server -f -"
check deny  "real SSA of Cluster hydra-mgmt" -- bash -c "echo '$(obj $C Cluster hydra-mgmt "$mg" '{"x":1}')' | $K $SA apply --server-side --field-manager=hydra-hydra-mgmt --force-conflicts -f -"
check allow "dry-run patch KCP hydra-mgmt-cp" -- $K $SA patch kubeadmcontrolplanes hydra-mgmt-cp --type merge -p '{"spec":{"replicas":5}}' --dry-run=server
check deny  "patch KCP hydra-mgmt-cp (root on mgmt nodes)" -- $K $SA patch kubeadmcontrolplanes hydra-mgmt-cp --type merge -p '{"spec":{"preKubeadmCommands":["evil"]}}'
check deny  "patch KCP hydra-mgmt-cp to remove its label" -- $K $SA patch kubeadmcontrolplanes hydra-mgmt-cp --type merge -p '{"metadata":{"labels":{"hydra.io/cluster":null}}}'
check deny  "patch unlabelled leftover hydra-mgmt-cp-pdh (by name)" -- $K $SA patch hydramachinetemplates hydra-mgmt-cp-pdh --type merge -p '{"spec":{"x":1}}'
check deny  "delete unlabelled leftover hydra-mgmt-cp-pdh" -- $K $SA delete hydramachinetemplates hydra-mgmt-cp-pdh
check deny  "create MD 'evil' joining hydra-mgmt by spec.clusterName" -- bash -c "echo '$(obj $C MachineDeployment evil '{}' '{"clusterName":"hydra-mgmt"}')' | $K $SA create -f -"
check deny  "create KCT 'evil' with CAPI cluster-name=hydra-mgmt" -- bash -c "echo '$(obj $KCTV KubeadmConfigTemplate evil '{"cluster.x-k8s.io/cluster-name":"hydra-mgmt"}' '{}')' | $K $SA create -f -"
echo "== workload clusters: CI deploys them"
check allow "patch KCP hydra-wl0-cp" -- $K $SA patch kubeadmcontrolplanes hydra-wl0-cp --type merge -p '{"spec":{"replicas":5}}'
check allow "create Cluster hydra-wl1" -- bash -c "echo '$(obj $C Cluster hydra-wl1 '{"hydra.io/cluster":"hydra-wl1"}' '{}')' | $K $SA create -f -"
check allow "delete unused wl0 template (apply prunes)" -- $K $SA delete hydramachinetemplates hydra-wl0-md-0
echo "== addon proxies select only their own cluster"
check allow "HCP hydra-wl0-cilium as hydra renders it" -- bash -c "echo '$(obj $HCPV HelmChartProxy hydra-wl0-cilium "$wl" '{"clusterSelector":{"matchLabels":{"cni.hydra.io/cilium":"hydra-wl0"}}}')' | $K $SA create -f -"
check deny  "HCP labelled wl0 selecting hydra-mgmt" -- bash -c "echo '$(obj $HCPV HelmChartProxy x1 "$wl" '{"clusterSelector":{"matchLabels":{"hydra.io/cluster":"hydra-mgmt"}}}')' | $K $SA create -f -"
check deny  "HCP with matchExpressions" -- bash -c "echo '$(obj $HCPV HelmChartProxy x2 "$wl" '{"clusterSelector":{"matchLabels":{"cni.hydra.io/cilium":"hydra-wl0"},"matchExpressions":[{"key":"a","operator":"Exists"}]}}')' | $K $SA create -f -"
check deny  "HCP with an empty selector (selects every cluster)" -- bash -c "echo '$(obj $HCPV HelmChartProxy x3 "$wl" '{"clusterSelector":{}}')' | $K $SA create -f -"
check deny  "HCP without its cluster label" -- bash -c "echo '$(obj $HCPV HelmChartProxy x4 '{}' '{"clusterSelector":{"matchLabels":{"a":"b"}}}')' | $K $SA create -f -"
check deny  "HCP with a second, foreign matchLabel" -- bash -c "echo '$(obj $HCPV HelmChartProxy x5 "$wl" '{"clusterSelector":{"matchLabels":{"cni.hydra.io/cilium":"hydra-wl0","other":"hydra-mgmt"}}}')' | $K $SA create -f -"
check allow "HCP metrics-server style (hydra.io/cluster: own)" -- bash -c "echo '$(obj $HCPV HelmChartProxy hydra-wl0-ms "$wl" '{"clusterSelector":{"matchLabels":{"hydra.io/cluster":"hydra-wl0"}}}')' | $K $SA create -f -"
check deny  "HCP selecting by a shared value (reviewer: true/true)" -- bash -c "echo '$(obj $HCPV HelmChartProxy x6 '{"hydra.io/cluster":"true"}' '{"clusterSelector":{"matchLabels":{"anykey":"true"}}}')' | $K $SA create -f -"
check deny  "HCP with an empty own label" -- bash -c "echo '$(obj $HCPV HelmChartProxy x7 '{"hydra.io/cluster":""}' '{"clusterSelector":{"matchLabels":{"hydra.io/cluster":""}}}')' | $K $SA create -f -"
check deny  "delete an admin-made HCP that selects hydra-mgmt (uninstalls its chart)" -- $K $SA delete helmchartproxies foreign-proxy
check deny  "re-point an admin-made HCP away from hydra-mgmt" -- $K $SA patch helmchartproxies foreign-proxy --type merge -p '{"spec":{"clusterSelector":{"matchLabels":{"cni.hydra.io/cilium":"hydra-wl0"}}}}'
check allow "delete a wl0 HCP as hydra renders it (prune)" -- $K $SA delete helmchartproxies hydra-wl0-old
check deny  "HCP with two allowed keys" -- bash -c "echo '$(obj $HCPV HelmChartProxy x8 "$wl" '{"clusterSelector":{"matchLabels":{"hydra.io/cluster":"hydra-wl0","cni.hydra.io/cilium":"hydra-wl0"}}}')' | $K $SA create -f -"
echo "== no cluster may take hydra-mgmt's control-plane address"
check deny  "new HydraCluster claiming 192.168.15.11" -- bash -c "echo '$(obj infrastructure.cluster.x-k8s.io/v1beta1 HydraCluster hydra-wl9 '{"hydra.io/cluster":"hydra-wl9"}' '{"controlPlaneEndpoint":{"host":"192.168.15.11","port":6443}}')' | $K $SA create -f -"
check allow "dry-run of the same (plan sees it, apply cannot)" -- bash -c "echo '$(obj infrastructure.cluster.x-k8s.io/v1beta1 HydraCluster hydra-wl9 '{"hydra.io/cluster":"hydra-wl9"}' '{"controlPlaneEndpoint":{"host":"192.168.15.11","port":6443}}')' | $K $SA create --dry-run=server -f -"
check allow "new HydraCluster on its own address" -- bash -c "echo '$(obj infrastructure.cluster.x-k8s.io/v1beta1 HydraCluster hydra-wl8 '{"hydra.io/cluster":"hydra-wl8"}' '{"controlPlaneEndpoint":{"host":"192.168.15.12","port":6443}}')' | $K $SA create -f -"
echo "== RBAC still bounds it"
check deny  "delete Cluster hydra-wl1 (no delete on Clusters)" -- $K $SA delete clusters.cluster.x-k8s.io hydra-wl1
check deny  "read Secret hydra-mgmt-kubeconfig" -- $K $SA get secret hydra-mgmt-kubeconfig
check deny  "edit the guard policy itself" -- kubectl --context kind-$NAME $SA delete validatingadmissionpolicy hydra-ci-guard
echo "== a person is not affected"
check allow "admin patches KCP hydra-mgmt-cp" -- $K patch kubeadmcontrolplanes hydra-mgmt-cp --type merge -p '{"spec":{"replicas":3}}'
echo "pass=$pass fail=$fail"
[ $fail -eq 0 ]
