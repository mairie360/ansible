# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Provisions the Mairie360 machines and bootstraps Kubernetes on each of them (k3s + Cilium +
WireGuard + Argo CD). It does **not** own application state: what gets deployed on top lives in
the sibling repo `../Deploiment` (GitOps, Argo CD watches it). This repo stops once an instance is
registered in its group's Argo CD and its secrets are sealed; Argo CD takes over from there.

Clone `Deploiment` next to this repo (`Devops/ansible` + `Devops/Deploiment`): phase 4 writes each
instance's `secrets.yaml` there. `README.md` documents the model, the phases, the secrets flow and
the procedures in more depth than is worth duplicating here; read it first. The repository is
public: no address, key or token in a tracked file (CI checks both).

## Model: one Argo CD + its instances, per group

A *group* (`org_id`, e.g. `mairie360`, `client-example`) is one Argo CD machine plus the instance
machines it drives, each a single-node k3s cluster. A group's Argo CD only reaches the instances of
its own group, over a group-private WireGuard tunnel: that tunnel, not an application-level check,
isolates one client from another.

- `org_id` keeps dashes (it must match `Deploiment`'s `clusters/<org_id>/`); Ansible group names
  only accept underscores, so `org_group` (`inventory/group_vars/all.yml`) is the bridge. Always
  index `groups[...]` with `org_group`, never `org_id`.
- `env_name` is the cluster's name in Argo CD and must equal the
  `clusters/<org>/instances/<env_name>/` folder in `Deploiment`; any other name leaves the
  Application stuck on "cluster does not exist". `k8s_instance_link` enforces it.
- `ansible_host` is for SSH; `wg_ip` is for Argo CD (port 6443, only open on `wg0` to the group's
  Argo CD).
- The real inventory `inventory/hosts.yml` is gitignored (MAIR-708); `hosts.example.yml` is the
  template.

## The phases (`playbooks/site.yml`)

Each play carries its phase tag (MAIR-244): `--tags phase1,phase1b,phase2` runs those plays only.

| Tag | Role | Target | Does |
|---|---|---|---|
| `phase1` | `k8s_node` | `*_argocd:*_instances` | hardening, UFW (instances open 80/443, Argo CD nothing), k3s (Traefik, flannel and the built-in NetworkPolicy disabled, ServiceLB on), Cilium + Hubble |
| `phase1b` | `wireguard` | `*_argocd:*_instances` | group tunnel: the Argo CD machine listens, instances dial out |
| `phase2` | `k8s_argocd` | `*_argocd` | Argo CD, `platform-app.yaml`, the group's instances ApplicationSet, `ImageUpdater`s of dev / staging, GHCR and Deploiment write-back credentials |
| `phase3` | `k8s_instance_link` | `*_instances` | `argocd cluster add` over the WireGuard IP, cluster labels (`mairie360.fr/role=instance`, `org`, `env`, `ingress`, tag `labels`) and the `mairie360.fr/revision` annotation, instance kubeconfig on the Argo CD machine |
| `phase4` (`playbooks/secrets.yml`) | `k8s_instance_secrets` | `*_instances` | seals the instance secrets with `Deploiment/scripts/seal-secrets.sh` on the Argo CD machine, copies `secrets.yaml` back, backs up the generated values and the sealing key |
| `phase5` | `github_runner` | `*_argocd` | Promote workflow runner of `Deploiment` (installed with `github_runner_enabled`, removed otherwise; also tag `github_runner`) |

`k8s_argocd` installs neither cert-manager, nor an ingress controller, nor a ClusterIssuer: Argo CD
deploys them on the instances from `Deploiment` (duplicates would conflict). Always `--limit` whole
groups (`'<org>_argocd:<org>_instances'`): the `wireguard` role fails when a machine of the group is
missing from the play.

**Disaster recovery** does not run `site.yml` in one go: phases 1, 1b, 2, then the old sealing key
back with `scripts/restore-sealing-key-file.sh`, then phase 3, never phase 4 (it would seal new
passwords). Runbook: `Deploiment` `docs/en/disaster-recovery.md`.

## Commands

```bash
ansible-galaxy collection install -r requirements.yml   # once

ansible-playbook playbooks/site.yml                                         # everything
ansible-playbook playbooks/site.yml --limit '<org>_argocd:<org>_instances'  # one group
ansible-playbook playbooks/site.yml --limit '<org>_argocd:<org>_instances' --tags phase3
ansible-playbook playbooks/site.yml --tags labels                           # cluster labels only

ansible-playbook playbooks/secrets.yml --limit mairie360-dev                # phase 4, one instance
RESEND_API_KEY=re_xxx ansible-playbook playbooks/secrets.yml --limit mairie360-dev

ansible-playbook playbooks/verify.yml   # acceptance test (runs Deploiment/scripts/verify.sh), GDPR checks
ansible-playbook playbooks/site.yml -e wireguard_close_public_ssh=true   # once the tunnel is confirmed
```

## Checks (CI: `.github/workflows/checks.yml`)

Run the relevant ones before pushing a role change:

```bash
python3 tests/check_no_public_ip.py          # no public IPv4 in a tracked file (MAIR-708)
python3 tests/check_no_log.py                # secret-handling tasks set no_log (MAIR-599)
ansible-lint                                 # production profile; known debt in .ansible-lint-ignore
ansible-playbook tests/seal_passwords_test.yml        # RESTIC_PASSWORD / ADMIN_PASSWORD resolution
ansible-playbook tests/image_updater_test.yml         # ImageUpdater templates (MAIR-186, MAIR-444)
ansible-playbook tests/github_runner_test.yml         # promotion runner (MAIR-346)
python3 tests/check_image_updater_services.py         # image_updater_services vs Deploiment's values
bash tests/mair_244_site_phase_tags_test.sh           # each phase tag selects its play only
bash tests/mair_244_restore_sealing_key_test.sh       # sealing key restore (PyYAML)
bash tests/mair_677_secret_scan_test.sh               # Gitleaks rules (docker)
```

`tests/mair_244_sealing_key_drill.sh` is a manual drill on kind clusters (docker, kind, helm,
kubeseal). `playbooks/verify.yml` is the only end-to-end check and needs provisioned machines.
Review UFW and k3s config changes carefully: a bad rule locks a machine out. Run logs go to
`logs/` (gitignored, they can hold machine output).

`tests/check_no_log.py` flags tasks by variable/file *name*. A variable whose name looks secret but
holds a path or a flag goes in its `SAFE_NAMES`; a template that embeds a secret goes in
`SECRET_TEMPLATES`. Never print a secret through `debug`, and never pass a whole `hostvars` entry
around (it holds the instances' secrets).

## Secrets

- Phase 4 runs `seal-secrets.sh` **only from the Argo CD machine**, the only one reaching each
  instance API server through the tunnel.
- External secrets resolve in this order: `instance_secrets[<host>].<key>` (`-e @secrets.yml
  --ask-vault-pass`) → environment variable (`RESEND_API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `COCKPIT_TOKEN`, `ADMIN_EMAIL`) → the value already
  sealed on the instance → a prompt (`-e secrets_prompt=false` in CI only reports). The admin
  e-mail is mandatory, and so is the Resend key at the first sealing.
- `RESTIC_PASSWORD` and the first `ADMIN_PASSWORD` are resolved by the role (instance → backup file
  → generated) and backed up in `operator_backup_dir` (`~/.mairie360`, mode 700) with the sealing
  key (`sealing-keys/<org>-<env>.yaml`). A sealed value is never replaced. All three go to the
  team vault after each phase 4 run: without the sealing key a reinstall cannot decrypt the
  committed `secrets.yaml`; without `RESTIC_PASSWORD` the backups are unreadable.
- The generated `secrets.yaml` lands uncommitted in `../Deploiment/clusters/<org>/instances/<env>/`:
  commit and push it (staging and prod then go through Deploiment's Promote). Until then the pods
  stay in `CreateContainerConfigError`, which is expected.

## Adding a client group

No playbook or role change:

1. `inventory/hosts.yml`: `<org>_argocd` and `<org>_instances` groups (shape in
   `hosts.example.yml`), with a WireGuard subnet of its own.
2. `inventory/group_vars/<org>_argocd.yml` and `<org>_instances.yml`: `org_id: <org>`.
3. In `Deploiment`: `clusters/<org>/instances/<env>/values.yaml` and `compliance/<org>/`.

## Gotchas

- k3s stays `NotReady` until Cilium is installed (no CNI before `tasks/cilium.yml`): expected
  mid-phase-1.
- `k8s_node` leaves port 22 open on purpose; only `wireguard_close_public_ssh=true` removes it, and
  only on machines that answered over the tunnel.
- Pinned versions (`k3s_version`, `argocd_version`, `cilium_*`, `hubble_cli_version`,
  `kubeseal_version`, `github_runner_version`) live in `inventory/group_vars/all.yml` and move
  through a PR. k3s upgrades one minor at a time and one machine at a time (README, "Upgrading k3s
  and Argo CD").
- `ingress_controller` (`nginx` by default) becomes the `mairie360.fr/ingress` cluster label in
  phase 3; switch a machine with the host var + `--tags labels`, together with
  `global.ingressController` in its Deploiment values (Deploiment ADR 0001).
- `deploiment_env_revisions` (dev → `main`, staging / prod → their branches) and
  `deploiment_auto_sync_envs` (`dev`, `staging`) only take effect after re-running `k8s_argocd`.
- `image_updater_services` (`roles/k8s_argocd/defaults/main.yml`) must follow the instance keys of
  Deploiment's values (`tests/check_image_updater_services.py`); the `project-front` key vs the
  `projects-front` image is intended.
- Prose and comments: English. The README still has French sections, translated when touched.

## Pull request reviewers

`.github/CODEOWNERS` requests the whole team on every PR (`CarolinHugo`, `LAURETbenjamin`,
`MathTek`, `Quentintnrl`; GitHub never requests the author).
