# hydra-deployer

Plans `clusters/*/hydra.yaml` changes on pull requests and applies them after merge (PET-102,
ADR-002 decision 6 as amended). It is a **pull-based agent in the lab**: a CronJob on hlcluster that
reads only `hydra.yaml` files from GitHub and runs the pinned `hydra` binary's own `plan` and `apply`.
No repository code runs in the lab, which matters because this repository is public and has many
writers.

## What it does, every 2 minutes

- **Plan.** For each open pull request **from this repository** (forks are skipped) that changes a
  `clusters/<name>/hydra.yaml`, it runs `hydra cluster plan`, posts one comment with the plan per
  cluster, and sets a commit status `hydra/plan`:
  - `success`: the plan applies on merge;
  - `failure`: apply would refuse (`!` lines);
  - `error`: plan failed.

  It plans again when the PR's head moves, or ten minutes after a plan that failed.

  **The comment is for reading only.** The agent records what it planned in its own state: the PR's
  head, the sha256 of each `hydra.yaml`, and each plan's hash. Nothing typed into a comment can approve
  anything.
- **Apply.** For each cluster on `main`, when the newest commit touching its `hydra.yaml` has not been
  applied, it plans `main` again.
  - **No changes:** the commit is recorded as applied.
  - **Changes:** the agent applies only if all of these hold:
    - the commit came from a PR **merged into `main`**;
    - the agent planned that PR **at its final head**;
    - the `hydra.yaml` is **byte for byte the one it planned**;
    - **the plan is the same now** (a different plan means something drifted);
    - there is no refusal;
    - **an approver** (`APPROVERS`, today `Eyasluna`) merged the PR, or approved it at that head.
  - **Order:** workload clusters first. `hydra-mgmt` goes last, after an etcd backup Job, and not at
    all in a run where a workload apply failed.
  - **Reporting:** it sets the commit status `hydra/apply/<cluster>` and comments the result on the PR.
  - **Failure:** a failed apply is retried with backoff (2 minutes, doubling, at most an hour). A
    blocked commit is reported once and left alone.
- **Identity check:** a `hydra.yaml` whose `metadata.name` is not its directory's name, or whose
  namespace is not `default`, is never planned or applied. The validator refuses it too.
- **Never:** it never deletes a cluster, passes `--prune-pools` or `--allow-remove`, or applies a
  direct push to `main`. Its hydra-mgmt identity cannot delete Clusters, control planes, pools or
  health checks at all.

State lives in the ConfigMap `hydra-deployer/hydra-deployer-state` on hlcluster, with keys
`<cluster>.applied`, `.blocked`, `.pending`, `.failures` and `.retry-after`.

## Setup (once)

Root is not needed. Each step names its cluster.

1. **hydra-mgmt: the agent's identity.** It must be created by a person, since it is a new
   credential:

   ```bash
   kubectl --kubeconfig ~/.kube/hydra-mgmt.yaml apply -f deployer/manifests/hydra-mgmt-access.yaml
   ```

   Then copy its token and CA into hlcluster, without printing either:

   ```bash
   kubectl create namespace hydra-deployer --dry-run=client -o yaml | kubectl apply -f -   # hlcluster context
   kubectl --kubeconfig ~/.kube/hydra-mgmt.yaml -n hydra-deployer-access get secret hydra-deployer-token -o json \
     | jq '{apiVersion:"v1",kind:"Secret",type:"Opaque",
            metadata:{name:"hydra-deployer-mgmt-api",namespace:"hydra-deployer"},
            data:{token:.data.token,"ca.crt":.data["ca.crt"]}}' \
     | kubectl apply --server-side --field-manager=hydra-deployer-access -f -   # hlcluster context
   ```

2. **GitHub token.** Create a **fine-grained personal access token**:
   - resource owner `Petatron`, repository access **only** `Petatron/hydra-gitops`;
   - permissions: **Contents: read**, **Pull requests: read and write**, **Commit statuses: read and
     write**; nothing else;
   - an expiry you will rotate at.

   Put it in hlcluster without it reaching your shell history (paste, then Ctrl-D):

   ```bash
   kubectl -n hydra-deployer create secret generic hydra-deployer-github --from-file=token=/dev/stdin
   ```

3. **hlcluster: the agent.** From this repository's root:

   ```bash
   kubectl apply -k deployer/   # hlcluster context
   ```

4. **Verify:**

   ```bash
   kubectl -n hydra-deployer create job --from=cronjob/hydra-deployer first-run
   kubectl -n hydra-deployer logs job/first-run
   ```

   - **First run:** each cluster whose live state already matches `main` is recorded as applied ("needs
     no changes").
   - **Then:** a PR changing a `hydra.yaml` gets a plan comment within about 2 minutes.

## Operating it

| Situation | What to do |
|---|---|
| A commit is **blocked** (drift, refusal, no reviewed plan, direct push) | Check `hydra cluster plan` locally, then either apply by hand, or merge a follow-up PR (it gets a fresh plan). To retry the same commit: `kubectl -n hydra-deployer edit configmap hydra-deployer-state` and delete `<cluster>.blocked` |
| An apply keeps **failing** | The PR comment has the first failure's output; later retries only log (`kubectl -n hydra-deployer logs job/<latest>`). Fix the cause; the next retry picks it up. Rerunning apply is always safe (stateless) |
| **Pause** the agent | `kubectl -n hydra-deployer patch cronjob hydra-deployer -p '{"spec":{"suspend":true}}'`; resume with `false` |
| **Upgrade hydra** | Bump the pin in `scripts/ci/install-tools.sh` and `tool-checksums.txt`, and `HYDRA_VERSION`/`HYDRA_SHA256` in `manifests/hlcluster.yaml` (a test keeps them equal), then `kubectl apply -k deployer/` |
| Add a **cluster** | **First** add its kubeconfig and credentials Secret names to `resourceNames` in `manifests/hydra-mgmt-access.yaml` and apply it to hydra-mgmt; otherwise its first apply fails on the kubeconfig and retries hourly. Add it to `MANAGEMENT_CLUSTERS`/`BACKUP_CRONJOBS` if it is a management cluster |
| Two PRs change **one cluster** before the first is applied | Only the newest commit is considered, and its plan (made before the first was applied) no longer matches, so it is blocked. Apply by hand, or merge a no-op follow-up to get a fresh plan |

## Rotating credentials

- **GitHub token:** create the new token (step 2), then
  `kubectl -n hydra-deployer delete secret hydra-deployer-github` and create it again. Revoke the old
  token on GitHub.
- **hydra-mgmt token:** delete the token Secret on hydra-mgmt
  (`kubectl -n hydra-deployer-access delete secret hydra-deployer-token`), re-apply
  `hydra-mgmt-access.yaml`, which makes a new token, then repeat the copy in step 1. Deleting the
  Secret revokes the old token.

## Recovering

The agent keeps no state outside its ConfigMap.
- **hlcluster lost or rebuilt:** repeat the setup. Losing the ConfigMap is safe: the next run records
  every cluster that matches `main` as applied, and blocks one that does not match (no reviewed plan),
  for a person to look at.
- **If hlcluster is down:** nothing is planned or applied. Local `hydra cluster plan/apply` works as
  always.
