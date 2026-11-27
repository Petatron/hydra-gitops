# hydra deploy from GitHub Actions

`.github/workflows/hydra-deploy.yaml` (PET-102) plans `clusters/*/hydra.yaml` changes on pull
requests and applies them after merge, from a GitHub-hosted runner. The runner reaches the
management cluster's API through a Tailscale subnet route, and is allowed to reach nothing else in
the lab. The decisions are in `scripts/ci/hydra_deploy.py`; its tests run in the validate workflow.

## What happens

- **Pull request into `main`** (same repository; PRs from forks are never planned). The workflow,
  running `main`'s files, plans every `hydra.yaml` the PR changes, as it stands at the PR's head.
  It posts one comment with the plans and sets the commit status `hydra/plan`. Only then does it
  record the plans, as the artifact `hydra-plan-<head sha>`. Each push to the PR plans again.
  - `success`: merging applies the plans, except for a plan-only cluster.
  - `failure`: a person has to look first. One of these happened:
    - a plan has a `!` line: a refusal, or a note such as the one-time unprotected control-plane
      roll;
    - a `hydra.yaml` names a cluster other than its directory, or a namespace other than `default`.
  - `error`: planning failed.
- **Merge.** Every run evaluates **every** cluster as `main`'s tip has it, so a run that GitHub
  dropped while it waited, or a merge that started no run, loses nothing. A cluster whose plan shows
  no changes is left alone. For a cluster with changes, the run takes the plan recorded on the PR
  whose merge last changed its `hydra.yaml`, and applies only if all of these hold:
  - the plan artifact came from this workflow on a pull request, before the merge;
  - `hydra.yaml` on `main` is byte for byte the one planned at the PR's final head;
  - planning `main` again gives exactly the same plan, with no `!` line;
  - that plan was never applied before. Each apply first marks the PR's head commit with the
    status `hydra/apply/<cluster>`, set by the workflow's own token, and a marked plan is never
    applied again automatically. A change rolled back by hand is therefore not re-applied when the
    live cluster is back where the plan started.

  Anything else blocks that cluster, and the run fails with the reason. A different plan means the
  live cluster drifted, or something changed it after the review. A blocked cluster is reported
  again on every run until it is resolved: on the merged PR's own run as a comment, otherwise in the
  run's log. A cluster that drifted
  with no PR behind the change blocks too, which makes every run a drift check.
- **Results** go in a comment on the merged PR, and in the run.
- **Merging is the approval.** A comment is for reading only; nothing written in one approves
  anything.

What it never does:
- pass `--prune-pools`, `--allow-remove`, `--adopt` or `--force-conflicts`;
- delete a cluster (removing a `hydra.yaml` deletes nothing);
- apply a direct push to `main`.

**hydra-mgmt is plan-only.** It is the cluster the workflow acts through, and its changes go after
an etcd backup, which CI cannot take. Its PRs get a plan. After merging, take the backup, then run
`hydra cluster apply` by hand. The admission policy below makes this a rule, not a convention.

## What the identity can do

Read this before giving the token to anything. `plan` is a server-side dry-run apply, and
Kubernetes authorizes a dry run like the real write. So the token holds write rights on the
Cluster API objects in `default`, and RBAC cannot narrow that.

- **Workload clusters (hydra-wl0): effectively admin.** Changing a control plane or bootstrap
  template runs commands on that cluster's nodes. Deploying those clusters is the token's job.
- **hydra-mgmt: plan only.** The ValidatingAdmissionPolicy `hydra-ci-guard` in
  `ci/hydra-mgmt-access.yaml` refuses every real (non-dry-run) create, update or delete by this
  identity of an object that is hydra-mgmt's. An object counts as hydra-mgmt's if any of these
  holds:
  - its `hydra.io/cluster` label is `hydra-mgmt`;
  - its `cluster.x-k8s.io/cluster-name` label is `hydra-mgmt`;
  - its `spec.clusterName` is `hydra-mgmt`;
  - its name is `hydra-mgmt` or starts with `hydra-mgmt-`. This covers older objects that carry no
    label.
- **Addon proxies select only their own cluster**, the way hydra renders them: exactly one
  `matchLabels` entry, keyed `cni.hydra.io/cilium` or `hydra.io/cluster`, whose value is the proxy's
  own non-empty `hydra.io/cluster` label. A proxy can't install a chart into hydra-mgmt.
- **A mistaken `controlPlaneEndpoint.host` equal to hydra-mgmt's address is refused.** This
  catches a mistake, not someone holding the token (see below).
- **What it can never do:**
  - delete a Cluster, control plane, MachineDeployment or health check;
  - read any Secret but the named ones (never hydra-mgmt's own kubeconfig);
  - change RBAC or the policy.
- **The hypervisor client key.** It can read `hydra-wl0-libvirt-pki`, because apply checks the
  hypervisor credentials with a full read.

`bash ci/test-policy.sh` tests all of this in a throwaway kind cluster, in 29 cases. Run it after
any change to `ci/hydra-mgmt-access.yaml`. The cases:
- dry runs against hydra-mgmt pass;
- real writes to hydra-mgmt are refused, including unlabelled older objects and a new
  MachineDeployment that joins by `spec.clusterName`;
- writes to wl0 pass;
- foreign, shared-value, empty or multi-key proxy selectors are refused, and so is a cluster
  claiming hydra-mgmt's address;
- RBAC still blocks deletes and Secrets;
- a person is unaffected.

**What the policy cannot remove.** Root on wl0's nodes, which the token has by design, is root on
VMs that share hydra-mgmt's hypervisor, storage pool (`k8s-workers`) and bridge (`br0`). From
there, an attacker could:
- announce hydra-mgmt's address on the bridge;
- reach the hypervisor with the readable `hydra-wl0-libvirt-pki` client key;
- possibly clone another VM's disk, if the provider takes any volume as a base image (not
  checked).

Separating the management cluster's network and hypervisor credentials closes that; a policy
can't. The token can also patch the `default` Namespace's labels, which apply needs.

## Setup (once)

1. **Network.** Done for PET-102:
   - the bastion advertises a `/32` route to the management API;
   - the Tailscale policy lets `tag:git-ci` reach only its port 6443;
   - the `lab-deploy` environment holds `TS_OIDC_CLIENT_ID` and `TS_OIDC_AUDIENCE`.

   `.github/workflows/lab-connectivity.yaml` re-checks this without credentials.
2. **The identity and its policy, on the management cluster.** A person creates them, since this
   is a new credential:

   ```bash
   kubectl --kubeconfig <management admin kubeconfig> apply -f ci/hydra-mgmt-access.yaml
   ```

3. **Its kubeconfig, as the `lab-deploy` secret `LAB_MGMT_KUBECONFIG`.** Build it from the token
   Secret and pipe it straight into `gh`, so the token is never printed or written to disk:

   ```bash
   kubectl --kubeconfig <management admin kubeconfig> -n hydra-ci-access get secret hydra-ci-token -o json \
     | jq -c --arg server 'https://<management API address>:6443' '.data as $d | {
         apiVersion: "v1", kind: "Config", "current-context": "hydra-ci",
         clusters: [{name: "management", cluster: {server: $server, "certificate-authority-data": $d["ca.crt"]}}],
         users: [{name: "hydra-ci", user: {token: ($d.token | @base64d)}}],
         contexts: [{name: "hydra-ci", context: {cluster: "management", user: "hydra-ci", namespace: "default"}}]}' \
     | gh secret set LAB_MGMT_KUBECONFIG --env lab-deploy --repo Petatron/hydra-gitops
   ```

   The server is the address the runner reaches through the route, the same one the clusters'
   `controlPlaneEndpoint` uses.
4. **Check the identity's rights** (as an admin, read-only):

   ```bash
   kubectl --kubeconfig <management admin kubeconfig> auth can-i --list -n default \
     --as system:serviceaccount:hydra-ci-access:hydra-ci
   ```

5. **Prove it:** open a PR that changes nothing a cluster would notice (a comment in a `hydra.yaml`).
   It should get a plan comment saying "No changes" for that cluster. Merging it records "no changes"
   and applies nothing.

## Operating it

| Situation | What to do |
|---|---|
| A cluster is **blocked** after merge | The run says why. Check `hydra cluster plan` locally. Then either apply by hand, or open a follow-up PR: it gets a fresh plan, and merging it applies that plan. Later runs report the block again until it is resolved |
| Two PRs for **one cluster** merged close together | The cluster is checked against the newer PR's plan. If that plan predates the first apply, it no longer matches and the cluster is blocked. Open a follow-up PR, or apply by hand |
| An apply **failed** | The run has hydra's output. Fix the cause, then apply by hand or open a follow-up PR. A plan is attempted at most once (`hydra/apply/<cluster>` on the PR's head), so re-running the run does not retry it; that is intended. Applying by hand is always safe to repeat |
| A change was **rolled back by hand** | Later runs block the cluster instead of re-applying the old PR's plan: it was applied once already. Merge a revert to make `main` match, or apply by hand |
| **Add a cluster** | First add its `<name>-kubeconfig` and `<name>-libvirt-pki` Secret names to `resourceNames` in `ci/hydra-mgmt-access.yaml`, and apply that by hand. Otherwise its apply fails when it reads them, possibly after creating machines. Nothing enforces this order |
| **Pause** deploys | Disable the workflow (Actions → hydra deploy → Disable). PRs still validate |
| **Upgrade hydra** | Bump the pin in `scripts/ci/install-tools.sh` and `tool-checksums.txt`; both workflows use it |

Each plan shows up as a `lab-deploy` deployment on the PR. That is GitHub recording which
environment the job used, not a deploy.

## Rotating credentials

- **The identity's token:** delete the Secret `hydra-ci-access/hydra-ci-token` on the management
  cluster (this revokes it). Re-apply `ci/hydra-mgmt-access.yaml`, which makes a new token, then
  repeat setup step 3.
- **Tailscale:** the federated identity has no secret. Revoking it in the Tailscale admin console
  stops every run from reaching the lab.
