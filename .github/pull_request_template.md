## Change and target

<!-- What changes, and why? Link the Linear issue. -->

- Target cluster: <!-- home / hydra-wl0 / management / CI-docs only -->
- Paths reconciled by Argo CD:

## Out-of-band steps

<!-- State "None" or list ordered steps with explicit cluster contexts.
Include management RBAC, credential creation/rotation, and node-group annotations.
Never paste credentials. -->

## Validation and rollout

- [ ] GitOps validation passed.
- [ ] Reviewed the target cluster, network settings, and prune/deletion impact.
- Verification after merge: <!-- Check actual behaviour, not only pod readiness. -->
- Rollback: <!-- Usually a revert PR; note any manual steps. -->

<!-- Merging to main causes automatic reconciliation on live clusters. -->
