# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Provisions the Mairie360 machines and bootstraps Kubernetes on each of them (k3s + Cilium +
WireGuard + Argo CD). It does **not** own application state: what gets deployed on top lives in
the sibling repo `../Deploiment` (GitOps, Argo CD watches it). This repo stops once an instance's
Argo CD is registered and its secrets are sealed; Argo CD takes over from there.

Clone `Deploiment` next to this repo (`Devops/ansible` + `Devops/Deploiment`) — phase 4 writes
each instance's `secrets.yaml` there, and phase 3 reads `clusters/<org>/instances/<env>/values.yaml`
from it (on the Argo CD machine, at `/opt/Deploiment`, checked out by `k8s_argocd`).

Read `README.md` first — it documents the model, the four phases, the secrets flow and the
conventions in more depth than is worth duplicating here.

## Model: one Argo CD + its instances, per group

A *group* (`org_id`, e.g. `mairie360`, `client-example`) is one Argo CD machine plus the instance
machines it drives, each a single-node k3s cluster. A group's Argo CD only ever reaches the
instances of its own group, over a group-private WireGuard tunnel — that tunnel, not any
application-level check, is what isolates one client from another.

- `org_id` keeps dashes (must match `Deploiment`'s `clusters/<org_id>/` folder); Ansible group
  names only accept underscores, so `org_group: "{{ org_id | replace('-', '_') }}"`
  (`inventory/group_vars/all.yml`) is the bridge — always index `groups[...]` with `org_group`,
  never `org_id`.
- `env_name` (dev/staging/prod) is the cluster's registered name in Argo CD and must equal the
  `clusters/<org>/instances/<env_name>/` folder name in `Deploiment`. Registering under any other
  name (e.g. `inventory_hostname`) leaves the Application stuck on "cluster does not exist" with
  no other symptom — `k8s_instance_link` enforces this.
- `ansible_host` is for SSH (the controller); `wg_ip` is for Argo CD (cluster-admin access, port
  6443, only ever reachable over `wg0`, never the public IP).
- The `mairie360.fr/role=instance` label (set by `k8s_instance_link`) is what the base
  ApplicationSets (sealed-secrets, cert-manager, ingress-nginx, cluster-issuer) select on; without
  it nothing deploys on that instance.

## The four phases (`playbooks/site.yml`)

| # | Role | Target | Does |
|---|---|---|---|
| 1 | `k8s_node` | `*_argocd:*_instances` | hardening, UFW (only `node_public_ports` open publicly; instances get 80/443, Argo CD gets none), k3s (Traefik + flannel + built-in NetworkPolicy disabled, ServiceLB on), Cilium CNI + Hubble |
| 1b | `wireguard` | `*_argocd:*_instances` | group tunnel: Argo CD machine listens, instances dial out |
| 2 | `k8s_argocd` | `*_argocd` | Argo CD, `platform-app.yaml`, the group's instances ApplicationSet, `argocd/ghcr-secret`, one `argocd-image-updater` alias per service (`image_updater_services` in `roles/k8s_argocd/defaults/main.yml`) |
| 3 | `k8s_instance_link` | `*_instances` | `argocd cluster add` over the WireGuard IP, cluster labels `mairie360.fr/role=instance`, `org`, `env`, `ingress` (tag `labels`) |
| 4 (`playbooks/secrets.yml`) | `k8s_instance_secrets` | `*_instances` | seals instance secrets (`Deploiment/scripts/seal-secrets.sh`, run from the Argo CD machine), copies `secrets.yaml` back to the local `Deploiment` checkout |

`k8s_argocd` deliberately does **not** install cert-manager, ingress-nginx or a ClusterIssuer —
Argo CD deploys those onto the instances from `Deploiment`; installing them here would create
conflicting duplicates (different versions, competing CRDs).

Always target whole groups with `--limit '<org>_argocd:<org>_instances'` — phase 1b's `wireguard`
role hard-fails (`assert`) if any machine of the group is missing from the play, because the
peer's public key can only come from another host in the same run.

## Commands

```bash
ansible-galaxy collection install -r requirements.yml   # once, before first use

ansible-playbook playbooks/site.yml                                  # everything
ansible-playbook playbooks/site.yml --limit '<org>_argocd:<org>_instances'  # one group only

ansible-playbook playbooks/secrets.yml                                # phase 4 alone
ansible-playbook playbooks/secrets.yml --limit mairie360-dev          # one instance
RESEND_API_KEY=re_xxx ansible-playbook playbooks/secrets.yml --limit mairie360-dev
ansible-playbook playbooks/secrets.yml -e secrets_open_pr=true        # + one Deploiment PR for the run

ansible-playbook playbooks/verify.yml   # acceptance test, fails the run on any check failure

ansible-playbook playbooks/site.yml -e wireguard_close_public_ssh=true   # after the tunnel is confirmed working
```

There is no CI and no linter config. Offline checks, to run before committing a role change:

```bash
python3 tests/check_no_log.py                          # static: secret-handling tasks must have no_log: true
ansible-playbook playbooks/site.yml --syntax-check     # same for secrets.yml / verify.yml
```

`playbooks/verify.yml` is the only end-to-end check and needs already-provisioned machines.
Review diffs carefully, especially in `tasks/main.yml` files that touch UFW rules or k3s config,
since a bad rule there can lock a machine out. Run logs go to `logs/` (`… | tee logs/<run>.log`),
gitignored because they can hold machine output.

`tests/check_no_log.py` flags tasks by variable/file *name* (`SECRET_NAME`, `SECRET_FILE`,
`SECRET_TEMPLATES`). A new variable whose name looks secret but only holds a path or a flag goes
in its `SAFE_NAMES`; a new template that embeds a secret goes in `SECRET_TEMPLATES`. Never print
a secret through `debug`, and never pass a whole `hostvars` entry around (it holds the instances'
secrets) — extract only the fields needed.

PRs get the whole team as reviewers through `.github/CODEOWNERS`.

## Secrets

- `.env` holds Scaleway credentials (gitignored, not committed) — source it or export the vars
  before running commands that touch Scaleway infrastructure directly (this repo's playbooks
  don't read it themselves; it's for adjacent tooling).
- `playbooks/secrets.yml` runs `seal-secrets.sh` **only from the Argo CD machine** — it's the only
  one with a route to each instance's API server through the tunnel.
- Generated secrets (`JWT_SECRET`, Postgres, Redis ACL, `RESTIC_PASSWORD`, `ADMIN_PASSWORD`) are
  created once and kept; nothing rotates automatically. `RESTIC_PASSWORD` and `ADMIN_PASSWORD` are
  resolved by the role itself, not by `seal-secrets.sh`, so it knows what it seals:
  `instance_secrets[<host>].restic_password|admin_password` → value on the instance → backup of an
  earlier run in `local_backup_dir` → new value. They deliberately have no environment variable
  (one value would be shared by every instance). `ADMIN_PASSWORD` is never rotated. `seal_extra_args` (role `k8s_instance_secrets`) can pass
  `--rotate-roles` (Postgres role passwords only) or `--rotate` (everything, including
  `RESTIC_PASSWORD` and the superuser) — read the warnings in `Deploiment/scripts/seal-secrets.sh`
  before using either.
- External secrets resolve in this order: `instance_secrets[<inventory_hostname>].<key>` (e.g.
  `-e @secrets.yml --ask-vault-pass`, gitignored) → matching environment variable
  (`RESEND_API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `COCKPIT_TOKEN`, `ADMIN_EMAIL`) → the value already sealed on the
  instance → an interactive prompt (skip with `-e secrets_prompt=false` in CI, which then only
  reports what's missing). `admin_email` is mandatory: an empty value fails the play.
- After a run, the generated `secrets.yaml` sits in `../Deploiment/clusters/<org>/instances/<env>/`
  uncommitted — it must reach Deploiment's `main` for Argo CD to deploy it. Until then, pods
  stay in `CreateContainerConfigError`; that's expected, not a bug. `-e secrets_open_pr=true`
  (`tasks/open_pr.yml`) does it: after the **last** instance of the `serial: 1` play (hence a
  `post_tasks` include in `playbooks/secrets.yml`, not a task in the role — that would open one PR
  per instance), it commits every re-sealed file in a temporary `git worktree` cut from
  `origin/<deploiment_repo_branch>`, pushes a `seal-secrets/<date>` branch and opens one PR with
  `gh` (reviewers `secrets_pr_reviewers`). The local Deploiment checkout is never touched and
  nothing is pushed to `main` directly. `staging`/`prod` follow their own Deploiment branches
  (`deploiment_env_revisions`) and only get the change through Deploiment's Promote workflow.
- `local_backup_dir` (`~/.mairie360/`, mode 0700, outside any git repo) holds what must survive
  the loss of an instance, all of it for the team vault: `sealing-keys/<org>-<env>.yaml` (without
  it every committed `secrets.yaml` of that instance is permanently undecryptable; a reinstalled
  machine cannot regenerate it), `restic-<org>-<env>.txt` (without it the backups are unreadable)
  and `admin-<org>-<env>.txt`. A changed value keeps the previous file as `*.<timestamp>~`.

## Adding a client group

Three files, no playbook or role changes:

1. `inventory/hosts.yml` — add `<org>_argocd` and `<org>_instances` groups (see the commented
   `client_example_*` template for the shape, including a dedicated WireGuard subnet like
   `10.10.1.0/24` so the new group has no network route to any other).
2. `inventory/group_vars/<org>_argocd.yml` and `inventory/group_vars/<org>_instances.yml` — one
   line each: `org_id: <org>`.
3. In `Deploiment`: `clusters/<org>/instances/<env>/values.yaml`.

## Other gotchas worth knowing before editing a role

- k3s stays `NotReady` until Cilium is installed (`k8s_node`'s `flannel-backend: none` +
  `disable-network-policy: true` means there is no CNI until `tasks/cilium.yml` runs) — this is
  expected mid-phase-1, not a failure.
- `k8s_node` leaves port 22 open publicly on purpose; only `wireguard_close_public_ssh=true`
  (after the tunnel is confirmed reachable) removes that rule, and only on machines it just
  pinged successfully — closing it earlier can lock you out permanently.
- Migrating an already-provisioned machine from flannel to Cilium (re-running `site.yml` on an
  older host) restarts every pod once — a few minutes of downtime. Do `dev` first.
- Pinned versions (`k3s_version`, `argocd_version`, `cilium_version`, `cilium_cli_version`,
  `hubble_cli_version`, `kubeseal_version`) live in `inventory/group_vars/all.yml` and are bumped
  through a PR, never picked up implicitly by re-running a playbook. k3s upgrades one minor at a
  time (the role refuses a bigger jump) and one machine at a time (`throttle: 1`); to cross
  several minors, loop with `-e k3s_version=<each minor's latest patch>` (README, "Upgrading k3s
  and Argo CD").
- Ingress controller (MAIR-260): `ingress_controller` (`nginx` default in `all.yml`, overridable
  per host) becomes the `mairie360.fr/ingress` cluster label in phase 3; Deploiment's Traefik
  AppSet selects `traefik`, the ingress-nginx one everything else. Switch a machine with the host
  var + `--tags labels`, together with `global.ingressController` in its Deploiment `values.yaml`
  (Deploiment `docs/adr/0001-replace-ingress-nginx.md`).
- `deploiment_env_revisions` (which Deploiment branch each env follows: dev → `main`,
  staging/prod → their own branches) and `deploiment_auto_sync_envs` (only `dev` auto-syncs) live
  in `all.yml` and only take effect after re-running `k8s_argocd`.
- `roles/k8s_argocd/defaults/main.yml`'s `image_updater_services` list must stay in sync with the
  instance keys under `APIs|BFFs|Fronts.instances.*` in `Deploiment`'s values files — note the
  `project-front` alias/key vs. the `projects-front` image name mismatch (documented there, not a
  typo to "fix").
