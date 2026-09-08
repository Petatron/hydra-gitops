# Validation schemas

The schema locations in `scripts/ci/validate.py` are pinned to immutable commits:

| Resources | Source |
| --- | --- |
| Kubernetes built-ins, both clusters v1.35.3 | [kubernetes-json-schema at 07b64c5](https://github.com/yannh/kubernetes-json-schema/tree/07b64c5376535fbbd6fb9910621e1a41f7613c14/v1.35.3-standalone-strict) |
| Argo Application, Cilium, MetalLB, Cluster API and other catalogued CRDs | [CRDs-catalog at 866b265](https://github.com/datreeio/CRDs-catalog/tree/866b2653a5334db9aed20ad74701e20fd464471b) |
| HydraCluster, HydraMachine, HydraMachineTemplate v1alpha1 | [Hydra CRDs at 29f14a3](https://github.com/Petatron/cluster-api-provider-hydra/tree/29f14a39bf3363e5978923e446730075fb709efd/config/crd/bases) |

Hydra schemas are generated from those upstream CRDs using kubeconform v0.8.0's
[openapi2jsonschema.py](https://github.com/yannh/kubeconform/blob/v0.8.0/scripts/openapi2jsonschema.py),
with `FILENAME_FORMAT='{kind}_{version}'` and otherwise default settings. The
provider and converter are Apache-2.0 licensed. Generated descriptions are kept
for diagnostics; do not edit these JSON files manually.

To refresh Hydra schemas from a checked-out provider at the intended revision:

```sh
# Download and inspect the converter linked above, then from this directory:
cd infrastructure.cluster.x-k8s.io
FILENAME_FORMAT='{kind}_{version}' python /path/to/openapi2jsonschema.py \
  /path/to/cluster-api-provider-hydra/config/crd/bases/*.yaml
```

Update the recorded provider commit with regenerated files in the same PR. For
Kubernetes or catalog updates, change the constants in the validator and the
source links above. Confirm cluster versions from inventory before changing
`KUBERNETES_VERSION`; current evidence is in hydra-bootstrap's
[home inventory](https://github.com/Petatron/hydra-bootstrap/blob/main/docs/phase0-current-cluster.md)
and [hydra-wl0 bootstrap](https://github.com/Petatron/hydra-bootstrap/blob/main/docs/cluster-bootstrap.md).
Run the acceptance tests and full repository validation after any schema update.

No `-ignore-missing-schemas` flag is used: introducing an uncatalogued kind
requires adding its real upstream schema. Catalog snapshots are structural
validation coverage, not assertions that those controller versions are installed.
The tests exercise Argo, Cilium, MetalLB, CAPI v1beta2, and Hydra v1alpha1 schemas.
Kubeconform does not evaluate CRD CEL or controller-side admission checks.
