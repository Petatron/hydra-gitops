# hydra-gitops — agent guide

> **Read first:** a merge to `main` **changes live Kubernetes clusters**, normally within about
> three minutes. There are no required status checks and no required reviews on `main` — nothing
> mechanical will stop a bad merge. Run the validator locally before every push.
>
> PR titles are `[PET-xx] Title`. Never put AI attribution in commits or PR bodies, but **do**
> prefix every PR comment you write with `**<Agent> (<Model>)**`. See
> [Working agreements](#working-agreements).

## What this repo is

Argo CD configuration for **two** Kubernetes clusters, each with its own root Application watching
`main`:

- **home** — the hand-built cluster. Root: `bootstrap/root-app.yaml` → `apps/` → `infrastructure/`.
- **hydra-wl0** — the workload cluster Hydra itself created. Root:
  `clusters/hydra-wl0/root-app.yaml` → `clusters/hydra-wl0/apps/`.

Applications self-heal drift and generally prune removed resources. The hydra-wl0 Cilium Application
deliberately **disables pruning and omits the deletion finalizer**, so losing that Application does
not take the CNI down with it. Preserve that.

```text
.github/            # validate workflow, PR template, CODEOWNERS
scripts/ci/         # validate.py + its failure-case tests, pinned tool installer
schemas/            # pinned CRD JSON schemas and their provenance
bootstrap/          # home cluster's one-time entry point (install.sh, root-app.yaml)
apps/               # home cluster Applications — Argo CD, Cilium, KEDA, MetalLB
infrastructure/     # home: namespaces, MetalLB pool, local-path storage
clusters/hydra-wl0/ # hydra-wl0 Applications — Cilium, storage, Cluster Autoscaler
  management-cluster/  # NOT reconciled by Argo CD — see below
```

## The three rules that matter most

### 1. Never point hydra-wl0 at the home tree

Do **not** apply top-level `apps/` or `bootstrap/root-app.yaml` to hydra-wl0. They are incompatible
in three concrete ways:

| | home | hydra-wl0 |
|---|---|---|
| Pod CIDR | `10.0.0.0/8` | `10.244.0.0/16` |
| kube-proxy | replaced by Cilium | still running |
| MetalLB | pool `192.168.15.200-250` | **none, deliberately** |

The home pod CIDR would not match what KCP gave kubeadm on hydra-wl0, and the MetalLB pool sits
inside the site's DHCP range — applying it risks address collisions on a live network. See
`clusters/hydra-wl0/README.md`.

The two GitOps roots describe the repo's *configuration*. They are **not** a claim that both roots
are currently installed or healthy — check the cluster before assuming.

### 2. `clusters/hydra-wl0/management-cluster/` must never be wired into an Application

Those manifests target the **management cluster**, a third target with no Argo CD managing it. The
Cluster Autoscaler running in hydra-wl0 uses two identities: in-cluster RBAC for workload Pods and
Nodes, and a **manually supplied kubeconfig** for Cluster API on the management cluster. That
management-side RBAC is applied out of band, on purpose. Adding it to an Application would have Argo
CD reconciling a cluster it does not own.

The Cluster Autoscaler also needs **DRA read access** (`resource.k8s.io` — `resourceclaims`,
`resourceslices`, `deviceclasses`); without it the autoscaler will not start at all, and the failure
presents as a node-group discovery miss rather than an RBAC error. Do not trim it as unused.

### 3. Validate locally — CI will not block you

```bash
python -m pip install -r scripts/ci/requirements.txt
bash scripts/ci/install-tools.sh "$PWD/.tools" && export PATH="$PWD/.tools:$PATH"
actionlint                                          # workflow syntax
python -m unittest discover -s scripts/ci -p 'test_*.py' -v   # prove the validator still rejects bad input
python scripts/ci/validate.py                       # all YAML/JSON + rendered Kustomize output
```

`validate.py` renders every Kustomization and checks it against pinned schemas — no cluster needed.
It also enforces source boundaries (no self-repository URL sprawl), rejects duplicate YAML keys, and
requires Helm image pins.

Schemas under `schemas/` are **generated, not hand-written** — pinned to immutable upstream commits
and regenerated with kubeconform's `openapi2jsonschema.py`. Refresh them per `schemas/README.md`;
never edit the JSON by hand. When the provider's CRDs change, the pinned Hydra schema revision has to
move with them or validation silently checks against the old shape.

## Opening a PR here

Fill in `.github/pull_request_template.md` honestly — it exists because this repo can break
production:

- **Target cluster** — home / hydra-wl0 / management / CI-docs only.
- **Paths reconciled by Argo CD** — which Applications pick this up.
- **Out-of-band steps** — say "None" or list ordered steps with explicit cluster contexts. Include
  management RBAC, credential creation or rotation, and node-group annotations. **Never paste
  credentials.**
- **Validation and rollout** — verification after merge must check *actual behaviour*, not merely
  that Pods are Ready. State the rollback; usually a revert PR, plus any manual steps.

`@Eyasluna` owns every path via CODEOWNERS. GitHub does not request a review from a PR's author, so
an agent pushing on that account's behalf should say in the PR description who still needs to look
at it.

## Project context

**Hydra** is an open-source on-prem Kubernetes lifecycle platform built on
[Cluster API](https://cluster-api.sigs.k8s.io/), with libvirt/KVM as the first infrastructure
provider and workload-driven node autoscaling via Cluster Autoscaler. It lives on the
[`Petatron`](https://github.com/Petatron) GitHub org across five repositories:

| Repo | Role |
|---|---|
| [`hydra`](https://github.com/Petatron/hydra) | Umbrella / namesake repo. Effectively empty today. |
| [`cluster-api-provider-hydra`](https://github.com/Petatron/cluster-api-provider-hydra) | The Cluster API **infrastructure provider**. Go, kubebuilder. This is where product code lives. |
| [`hydra-bootstrap`](https://github.com/Petatron/hydra-bootstrap) | **Documentation only.** Runbooks for standing up hosts, the CAPI management cluster, and workload clusters. |
| [`hydra-gitops`](https://github.com/Petatron/hydra-gitops) | Argo CD configuration. **A merge to `main` changes live clusters.** |
| [`hydra-infra`](https://github.com/Petatron/hydra-infra) | Terraform for the pre-Hydra, hand-built worker VMs. Legacy path, being superseded by the provider. |

**Where truth lives — three systems, deliberately separated:**

- **Notion** (PETATRON teamspace → *Project Hydra*) — architecture, ADRs, design docs, runbooks,
  worklogs, validation evidence. This is the source of truth for **engineering knowledge**.
- **Linear** (workspace `petatron`, team key `PET`) — issues `PET-5`..`PET-46+`. Source of truth
  for **delivery status**. `PET-1`..`PET-4` are onboarding stubs, not real work.
- **GitHub** — code, PRs, CI. Not a place to record decisions.

Durable findings go in Notion, **not** Linear comments. Every Linear issue must have a matching
Notion page, linked both ways.

## Working agreements

These are instructions, not suggestions.

### 1. PR titles carry the ticket; commit messages do not

```
PR title:  [PET-27] Publish template capacity for scale-from-zero
Commit:    feat: publish template capacity for scale-from-zero
```

The ticket goes in **square brackets at the front** of the PR title. Commit messages stay in
conventional style (`feat:` / `fix:` / `docs:` / `chore:` / `ci:`) with **no** ticket prefix.
`feat: … (PET-27)` is the wrong shape for a PR title — do not copy it.

**Every PR needs a ticket. If there isn't one, stop and ask.** When the work has no Linear (or Jira)
issue, do not open the PR on your own judgment — ask whether to create a ticket first or to open
this one without a prefix, and let the user decide. **Never invent or guess a number:** a wrong
`[PET-xx]` silently attaches the PR to somebody else's work and corrupts the tracking both systems
exist to provide.

### 2. Never put AI attribution in git history

Commit messages, commit trailers, PR titles, and PR bodies must **never** mention Claude, Codex,
Cursor, Copilot, or any AI assistant. Git history records what changed and why, not which tool
typed it. Never emit:

```
Co-Authored-By: Claude <noreply@anthropic.com>
Co-authored-by: Cursor <cursoragent@cursor.com>
🤖 Generated with [Claude Code](https://claude.com/claude-code)
Made with [Cursor](https://cursor.com)
```

Nor phrases like "AI-assisted", "generated by AI", or a model name anywhere in a commit message or
PR description. Write them as the human author would.

**Some tools inject attribution after the fact — always verify.** Writing a clean message is not
enough:

```bash
ATTRIB='^Co-authored-by:|Generated with \[|Generated with Claude|Made with \[|AI-assisted|generated by AI|noreply@anthropic\.com|cursoragent@cursor\.com|🤖'
git cat-file -p HEAD | grep -iE "$ATTRIB" && echo DIRTY || echo clean
gh pr view <n> --json body --jq '.body' | grep -iE "$ATTRIB" && echo DIRTY || echo clean
```

Match the *attribution strings*, **not** product names. Bare `claude`/`cursor` hit the filename
`CLAUDE.md`; bare `Claude Code` hits any commit that legitimately mentions the tool. Both report
clean commits as dirty.

`git commit --amend` re-injects the trailer and cannot fix this; rebuild the commit object with
`git commit-tree` instead. If the bad commit was already pushed, **ask before force-pushing**, then
use `--force-with-lease=<branch>:<old-sha>`.

### 3. DO attribute yourself in PR comments

This is the exception to rule 2, and it is required. Multiple agents (and the human maintainer)
review the same PRs; the comment thread has to say who is speaking.

**Prefix every GitHub PR comment, review comment, review summary, and reply you write with your
agent name and model in bold:**

```
**Claude (Opus 5)** — the informer starts on the first cached Get, so declining
the Watch bought nothing here. Read the Secret through mgr.GetAPIReader().
```

```
**Codex (GPT-5)** — agreed, but the RBAC verb also needs narrowing to `get`.
```

Format is `**<Agent> (<Model>)**` followed by an em dash. Use the name a reader would recognise —
`Claude (Opus 5)`, `Codex (GPT-5)`, `Cursor (Composer)`. If you genuinely do not know your model
identity, use `**<Agent>**` alone rather than guessing.

This applies to **comments only** — the conversation surface. It does **not** apply to the PR body,
the PR title, or commit messages, which stay clean under rule 2.

GitHub still attributes the comment to whichever account is authenticated. The prefix says which
agent wrote the text; do **not** claim the displayed GitHub author has changed.

### 4. Keep the record current continuously, not at the end

Whenever a ticket, a test, or a meaningful piece of progress completes, update **both**:

- **Linear** — status, plus a comment stating what was proven and, just as importantly, what was
  **not**.
- **Notion** — the linked page's worklog section and its Doc Status property.

Write down what was *learned* — especially anything that turned out different from what was
assumed — not just what shipped. The goal is that a future session resumes with no lost context.

Every operation performed against real infrastructure gets recorded as it happens, in both.

### 5. Read PR review comments via GraphQL, never REST

The REST endpoint `/pulls/N/comments` **silently omits threads** — it missed all eight of the
maintainer's review comments on one PR. Always use `reviewThreads`:

```bash
gh api graphql -f query='{repository(owner:"Petatron",name:"<repo>"){pullRequest(number:N){
  reviewThreads(last:80){nodes{isResolved path line
    comments(first:1){nodes{author{login} createdAt body}}}}}}}'
```

Filter by `createdAt` to find new threads. Do not slice by index against the REST count.

### 6. Expect several review rounds, and verify every comment

Copilot reviews every PR and has found dozens of real defects — several that would only have
surfaced in production as orphaned VMs or wedged finalizers. **Expect 3–5 review rounds per PR.**

Verify each comment against the actual file before acting on it. Some have been stale test
assumptions rather than code bugs, and at least one was a linter firing on a conversion that was
genuinely required. A review comment is evidence, not a verdict.

### 7. Generic design beats lab convenience

The reference lab (an XPS 13 control plane at home, a workstation at the office, ~30 ms apart) is
one person's setup, **not a requirement Hydra should be shaped around**. Hydra must assume nothing
about that hardware, topology, or site count.

When lab convenience and generic design diverge, **generic wins** — unless the shortcut is provably
free, meaning a config value that can change later, not a code path that would have to be undone.

Architecture decisions carry a **Scope** property in Notion: `Product` binds Hydra for everyone,
`Reference lab` describes only that setup. Do not read a lab ADR as a product constraint.
