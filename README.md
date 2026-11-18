# ansible

Provisioning of the Mairie360 machines. This repository **prepares the servers and
bootstraps Kubernetes**; the desired state of the applications lives in
[`mairie360/Deploiment`](https://github.com/mairie360/Deploiment).

## Model

**One Argo CD or one Mairie360 instance per machine.** A *group* = one Argo CD
+ its instances.

```
Group mairie360 (4 machines)       Group client-x (2 machines)
  mairie360-argocd                   client-x-argocd
  mairie360-dev                      client-x-prod
  mairie360-staging
  mairie360-prod
```

Each Argo CD only drives the instances of its group, and its WireGuard tunnel
only spans that group. A compromised client has no network route to another
one: the isolation comes from the topology.

## The seven phases

| Phase | Role | Target | What it does |
|---|---|---|---|
| 1 | `k8s_node` | all | Hardening, firewall (6443 only from the group's Argo CD, over `wg0`), k3s (Traefik disabled, ServiceLB on), Cilium CNI + Hubble |
| 1b | `wireguard` | all | Group tunnel: the Argo CD machine listens, the instances connect to it |
| 2 | `k8s_argocd` | `*_argocd` | Argo CD + `platform-app.yaml` + the group's ApplicationSet, `argocd/ghcr-secret`, `argocd/deploiment-git-creds`, one `ImageUpdater` per dev / staging instance |
| 3 | `k8s_instance_link` | `*_instances` | `argocd cluster add` on the WireGuard IP + labels (`mairie360.fr/role`, `org`, `env`, `ingress`) + annotation `mairie360.fr/revision` |
| 4 | `k8s_instance_secrets` | `*_instances` | Seals the instance secrets with `seal-secrets.sh`, asks for the missing external ones, copies `secrets.yaml` back to the local `Deploiment` checkout |
| 5 | `github_runner` | `*_argocd` (installs with `github_runner_enabled`, removes otherwise) | Ephemeral GitHub Actions runner of `Deploiment` and a least-privilege kubeconfig on staging, so the Promote workflow runs `verify.sh` before prod (MAIR-346) |
| 6 | `ops_runner` | `*_argocd` (installs with `ops_runner_enabled`, removes otherwise) | Ephemeral runner of this repository's Operations workflow: `secrets.yml` and `verify.yml` from the CI after approval (MAIR-803), see [Running from the CI](#running-from-the-ci-mair-803) |
| 7 | `hub_probe` | `*_argocd` (installs when `hub_probe_metrics_endpoint` is set, removes otherwise) | External probe: an OpenTelemetry Collector (`httpcheck`) checks `https://login.<instance_domain>/` of every instance each minute and pushes to the group's Cockpit source `<org>-probes`; its token is read from the vault (`/<org>/hub/cockpit-token`). Alert rules and e-mails: `mairie360/Infra` (MAIR-621) |
| 8 | `n8n_node` | `mairie360_n8n` (the n8n automation machine, outside the `*_argocd` / `*_instances` groups; `--limit mairie360_n8n`) | Host base only (MAIR-1075): packages, auditd, journald retention, UFW (22/80/443 open, the rest refused), fail2ban, unattended upgrades; checks that the stack of `mairie360/poc_n8n` is cloned and lists the running containers. It never deploys the stack (live secrets). Address: `terraform output n8n_machine` of `mairie360/Infra` |

The `k8s_argocd` role installs **neither** cert-manager, **nor** the ingress
controller, **nor** a ClusterIssuer: Argo CD deploys them on the instances,
from `Deploiment`. Installing them here would create conflicting duplicates
(different versions, competing CRDs). k3s's bundled Traefik is disabled for
the same reason: the controller version is pinned in `Deploiment`.

## Ingress controller per instance (MAIR-260)

ingress-nginx was retired upstream in March 2026; `Deploiment` replaces it
with a pinned Traefik (`bootstrap/appsets/traefik-appset.yaml`,
`docs/adr/0001-replace-ingress-nginx.md`). Only one controller can own ports
80/443 of a machine (ServiceLB), so each instance machine says which one it
runs with `ingress_controller` (`nginx` by default, set in
`inventory/group_vars/all.yml`). Phase 3 writes it as the Argo CD cluster
label `mairie360.fr/ingress`: the Traefik AppSet takes `traefik`, the
ingress-nginx AppSet everything else.

### Switching an instance to Traefik

Full procedure, verification and rollback: `docs/adr/0001-replace-ingress-nginx.md`
in `Deploiment`. The ansible part:

```bash
# 1. inventory/hosts.example.yml, on the instance host (dev first), then
#    `python3 scripts/restore-inventory.py --force` (MAIR-800):
#      mairie360-dev:
#        ...
#        ingress_controller: traefik
# 2. Re-apply the cluster labels only (no other task runs):
ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' --tags labels
```

Argo CD then deletes ingress-nginx from that machine and installs Traefik.
Flip `global.ingressController` in the instance's `values.yaml` in
`Deploiment` right after. Rollback: set `ingress_controller: nginx` again (or
remove it), same command, and revert the values.

## Promotion per environment (MAIR-345, MAIR-346)

Each environment follows its own `Deploiment` branch, set by
`deploiment_env_revisions` in `inventory/group_vars/all.yml` (`dev` →
`deploiment_repo_branch`, `staging` → `staging`, `prod` → `prod`). The
`Promote` workflow of `Deploiment` moves those branches (main → staging →
prod; staging merges instead of fast-forwarding when argocd-image-updater
committed tags on it, MAIR-444). That map is the single source for both layers:

- the **instance chart**: the instances ApplicationSet rendered by phase 2
  (`roles/k8s_argocd/templates/instances-appset.yaml.j2`) gives each
  Application its `targetRevision`;
- the **bootstrap layer** (cert-manager, ingress controller, sealed-secrets,
  cluster-addons): phase 3 writes the revision on each instance's Argo CD
  cluster Secret as the annotation `mairie360.fr/revision`, and the
  `bootstrap/` AppSets of `Deploiment` read it through their clusters
  generator. A controller version bump therefore reaches prod only through
  staging. Only the environments of `deploiment_revision_annotated_envs`
  (default `dev`, `staging`) are annotated; the others follow `main` until
  they are added (see the rollout below).

After changing `deploiment_env_revisions` or
`deploiment_revision_annotated_envs`, re-apply:

```bash
ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' --tags labels
ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances'   # re-renders the instances AppSet
ansible-playbook playbooks/verify.yml   # checks every cluster carries the expected annotation
```

`verify.yml` also checks, on every machine, the UFW state and the ports open to everyone, the age of
each WireGuard handshake (`verify_wg_handshake_max_seconds`, 600 by default), the k3s and Cilium
versions against their pins, fail2ban and its sshd jail, the swap size, and, on the workstation,
the local backup of each instance's sealing key (MAIR-242). The rules live in
`playbooks/templates/machine_gaps.j2`; CI runs `--syntax-check`, `yamllint` and `ansible-lint`.

### Rollout of MAIR-346

An annotated cluster reads `bootstrap/addons/` of `Deploiment` at its own
branch, and that directory only exists on a branch once the MAIR-346 change
of `Deploiment` has reached it. Annotating `prod` before that would point
its bootstrap Applications at a path that does not exist yet. Order:

1. Merge the `Deploiment` change on `main` (dev's bootstrap is unchanged: it
   follows `main` either way).
2. Promote `Deploiment` to `staging` (Promote workflow, `target=staging`);
   staging syncs by itself (`deploiment_auto_sync_envs`, MAIR-444).
3. Here: annotate `dev` and `staging` and set up the runner (the default
   `deploiment_revision_annotated_envs`), with the GitHub App variables of
   the next section:
   `ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' --tags labels,github_runner`,
   then `ansible-playbook playbooks/verify.yml`.
4. Promote `Deploiment` to `prod` (`target=prod`): the `verify-staging` job
   now runs `scripts/verify.sh` on this runner instead of the former
   checkbox.
5. Add `prod` to `deploiment_revision_annotated_envs` (PR), then
   `--tags labels` on every group. Client groups have a `prod` too and the
   list is global: their `Deploiment` revision is `prod` as well, so they
   switch at the same time.

### The promotion runner (phase 5)

Promoting to prod requires `scripts/verify.sh` to pass on staging, but
GitHub-hosted runners cannot reach the instance API servers (6443 is only
open on the WireGuard tunnel, to the group's Argo CD machine). Phase 5
(`roles/github_runner`) therefore installs a **self-hosted runner on the
Argo CD machine**, which already has that route. It is opt-in per group
(`github_runner_enabled`, only `true` in `group_vars/mairie360_argocd.yml`)
and never opens anything. Turning it off and re-running phase 5 removes what
an earlier run installed: the service, the App credential, the kubeconfigs and
the ServiceAccount, Roles and admission policy on each instance.

| | |
|---|---|
| Registration | Repository level on `mairie360/Deploiment`, labels `self-hosted`, `linux`, `mairie360-argocd-<org_id>`. A workflow targets it with `runs-on: [self-hosted, linux, mairie360-argocd-mairie360]` |
| Ephemeral | One just-in-time (JIT) registration per job: `gh-runner.service` registers, runs one job, exits and systemd restarts it. Before every job the runner directory is re-copied from the pinned release and its `HOME` (`/var/lib/gh-runner/home`) emptied, so nothing a job writes survives |
| User | `gh-runner`, non-root system user, no login shell, cannot write to `/home/gh-runner`. The systemd unit is sandboxed (`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, no access to `/root` nor `/etc/gh-runner`) |
| Network | Outbound HTTPS to GitHub only: no inbound port, no firewall or WireGuard change. The unit's `IPAddressDeny` blocks the tunnel subnet and this machine's pod and Service networks (Argo CD), except the WireGuard IPs of the verified instances |
| Instance access | For each env of `github_runner_verify_envs` (default `staging`): ServiceAccount `mairie360-ci/verify-runner` with exactly the rules listed at the top of `Deploiment/scripts/verify.sh` (no Secret read, no exec; mapped to its steps in `roles/github_runner/templates/verify-rbac.yaml.j2`), plus a `ValidatingAdmissionPolicy` that only lets it create, attach to and delete `verify-probe-*` pods of image `curlimages/curl:8.10.1` with no volume, env nor token, labelled at most `app.kubernetes.io/component` = `bff` or `keycloak-test` (`github_runner_probe_components`; never `api`, which would reach Postgres), with no command, lifecycle hook nor liveness/readiness/startup probe (the kubelet runs their commands in the container). With no Secret read, `verify.sh` skips its two plaintext checks there (argon2id `ADMIN_PASSWORD`, URL-safe passwords); run as root they still apply. Kubeconfig: `/home/gh-runner/.kube/instance-<env>.yaml` (root:gh-runner, 0440, one context `<env>`, current); the role fails when another account is a member of the `gh-runner` group, which could read the token. The admin kubeconfigs of `/root/.kube/` are never readable by the runner |
| Version | `github_runner_version` + `github_runner_sha256` in `inventory/group_vars/all.yml`. GitHub stops sending jobs to a runner more than 30 days behind: bump it at least monthly |

**Registration credential.** A GitHub App installed on `mairie360/Deploiment`
with the single repository permission **Administration: Read and write**.
Each job needs a new registration, so the App key stays on the machine, in
`/etc/gh-runner/app.pem` (root, 0600), read only by the root pre-start step
(`/usr/local/lib/gh-runner/prepare.sh`), which mints an installation token
scoped to that repository and permission, requests the JIT config and
revokes the token. Provide it once, the next runs keep it:

```bash
GITHUB_RUNNER_APP_ID=123456 GITHUB_RUNNER_APP_PRIVATE_KEY_FILE=~/keys/mairie360-runner.pem \
  ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' --tags github_runner
```

Without them (and without a key already on the machine), the play prompts
for the App ID and the key path; empty skips, and the runner is installed
but not started. Replace the key the same way; rotate the ServiceAccount
token by deleting `mairie360-ci/verify-runner-token` on the instance and
re-running `--tags github_runner`.

**Public repository.** `Deploiment` is public: only `workflow_dispatch`
workflows may target this runner (today: the `verify-staging` job of
`promote.yaml`), never `pull_request`, and the repo
setting *Actions → Fork pull request workflows* must require approval for
all outside collaborators. The sandbox and the admission policy bound what a
hijacked job could do, but they are a second line, not the first.

#### Creating the GitHub App (MAIR-799)

On 2026-11-06 the runner did not exist because no App could register it, so
the MAIR-794 fix had to be pushed straight onto `prod`. The App was created on
2026-11-13 (MAIR-1003, App ID `5736830`, installed on `mairie360/Deploiment`).
To create it again (new organization, lost App), an organization owner:

1. *Organization settings → Developer settings → GitHub Apps → New GitHub App*.
   Name e.g. `mairie360-promote-runner`, any homepage URL, **Webhook: Active
   unchecked**.
2. *Repository permissions → Administration: Read and write* (Metadata
   read-only is added by GitHub). No other permission, no organization or
   account permission, no event.
3. *Where can this GitHub App be installed: Only on this account*. Create.
4. Note the **App ID** at the top of the App page, then *Private keys →
   Generate a private key*: the `.pem` downloads once. Keep it in the team's
   password manager, never in a repository.
5. *Install App* → the organization → *Only select repositories* →
   `Deploiment`.
6. Run phase 5 with both values (command above). Then check:
   - on the machine, `systemctl status gh-runner` is `active` (or
     `activating` between two jobs) and `journalctl -u gh-runner` shows
     "registered ephemeral runner";
   - on GitHub, *Deploiment → Settings → Actions → Runners* lists a runner
     with the labels `self-hosted`, `linux`, `mairie360-argocd-mairie360`
     (it appears per job, since every registration is just-in-time);
   - `ansible-playbook playbooks/verify.yml --limit mairie360_argocd`
     passes, then a Promote to staging followed by `target=prod` runs
     `verify-staging` on it.

**Renewing the App key.** Generate a new private key on the App page, re-run
phase 5 with `GITHUB_RUNNER_APP_ID` and `GITHUB_RUNNER_APP_PRIVATE_KEY_FILE`
pointing to it (the role replaces `/etc/gh-runner/app.pem` and restarts the
service), check that the next job registers, then delete the old key on the
App page. A key that leaked is deleted on the App page first: the runner stops
registering until the new one is installed, nothing else breaks.

**Troubleshooting.**

| Symptom | Cause and fix |
|---|---|
| Promote waits forever on `verify-staging` ("Waiting for a runner") | `gh-runner` is not running or cannot register: `journalctl -u gh-runner`. A 401/404 from the token request means a wrong App ID, a deleted key or an App no longer installed on `Deploiment`. |
| The role prompts for the App ID on every run | `/etc/gh-runner/app.pem` or `app-id` is missing on the machine: pass both variables once. |
| `tar: …: Cannot mkdir: Function not implemented` while a job extracts an action | `RestrictSUIDSGID=` in the unit on systemd 259 / kernel 7.0 (fixed in MAIR-1003, `tests/github_runner_test.yml` pins it): re-run phase 5. |
| `verify.sh` fails with `forbidden` on a resource | The runner's Role lacks a rule `verify.sh` started to use: add it to `roles/github_runner/templates/verify-rbac.yaml.j2` (one rule per step) and re-run phase 5. |
| GitHub stopped sending jobs to the runner | The runner version is more than 30 days behind: bump `github_runner_version` / `github_runner_sha256`. |

`playbooks/verify.yml` runs `verify.sh` on those environments **as
`gh-runner` with its kubeconfig**, like the Promote workflow does, and fails
when `gh-runner.service` is not active. On the machine:
`journalctl -u gh-runner` (one "registered ephemeral runner" line per job).

`tests/github_runner_test.yml` (CI job `github-runner`) renders the
annotation, the runner's RBAC, admission policy and systemd unit on
localhost and pins the rules above.

Besides the rules listed in `verify.sh`'s header, the runner may `list` the
`Certificates` (cert-manager.io) and `Ingresses` of the instance namespace
(MAIR-795), the full objects (`spec` and `status` included), never a Secret:
`verify.sh` step 12c fails a Certificate whose HTTP-01 solver class is not the
one its Ingress asks for. Without that grant the step is skipped, so
**recreate any misaligned Certificate before applying it to an environment**,
or the next Promote's `verify-staging` fails on it. If a controller recreates
it (an Ingress with the cert-manager annotation), delete it with
`kubectl -n mairie360-<env> delete certificate <name>`; a Certificate managed
by hand does not come back (its Secret stays but renewal stops): make sure its
manifest is at hand, delete it, then apply the manifest again. Re-run
`--tags github_runner` to apply the grant to the existing instances.

## Running from the CI (MAIR-803)

`secrets.yml` and `verify.yml` also run from the **Operations** workflow
(`.github/workflows/ops.yml`), so that sealing an instance or checking it does
not depend on one operator's workstation and is logged in GitHub. `site.yml`
(machines, k3s, WireGuard, Argo CD) stays on the workstation. Actions →
Operations → *Run workflow* on `main`, with the group, `secrets` or `verify`
and the environment; a reviewer then approves the run in the environment
`ops-<org>-<env>`.

| | |
|---|---|
| Where | The group's Argo CD machine, runner of phase 6 (`roles/ops_runner`): same ephemeral JIT cycle and scripts as the promotion runner, registered on `mairie360/ansible` with labels `self-hosted`, `linux`, `mairie360-ops-<org_id>`, user `gh-ops` |
| What a job may run | Only `sudo -n /usr/local/sbin/mairie360-ops <secrets\|verify> <org> <env>` (sudoers; `sudo` keeps only the `SCW_*` variables). The wrapper checks its arguments, refreshes a root-owned clone of this repository to `origin/main` (reset and clean, the commit is printed) and runs the playbook from it: a job picks the operation and the instance, never the code |
| Inventory | `inventory/ci-<org>.yml` (tracked, WireGuard addresses only): the Argo CD machine is the controller and is reached locally; the instances over the tunnel as `ansible-ops`, with a key created on the Argo CD machine (`/etc/mairie360-ops/ssh/id_ed25519`, never copied off it), authorized on each instance only `from=` the Argo CD machine's WireGuard IP. `secrets.yml` never connects to the instances; `verify.yml` does (GDPR checks) |
| `secrets` | Runs `secrets.yml --limit <org>-<env> -e secrets_prompt=false` against a fresh clone of Deploiment's `main`, reading every value from the vault with the key of the `ci-ansible` Scaleway application (`mairie360/Infra`, `iam/`). The sealed `secrets.yaml` goes back through an artifact to a GitHub-hosted job, which opens a pull request `chore(<env>): seal secrets` on Deploiment with the release app (none when unchanged) |
| `verify` | Runs `verify.yml --limit <org>_argocd:<org>-<env>` |
| Network | `gh-ops.service` blocks the tunnel and this machine's pod and Service networks except the group's instances. Its sandbox is lighter than `gh-runner.service`: the wrapper runs as root inside the unit (no `NoNewPrivileges`, no read-only system) |

Values that are not in the vault cannot be typed in the CI: seal an instance's
first `RESEND_API_KEY` / `COCKPIT_TOKEN` from the workstation once (they are
then stored in the vault), or store them with `scw secret version create`.

**Setup, once per group** (an operator, from the workstation):

1. Install the runner's GitHub App on `mairie360/ansible` too (same App as the
   promotion runner, repository permission *Administration: Read and write*).
2. `ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' --tags ops_runner`
   (reuses the App credential of `/etc/gh-runner`, or `GITHUB_RUNNER_APP_ID` /
   `GITHUB_RUNNER_APP_PRIVATE_KEY_FILE`).
3. Create one environment per instance, `ops-<org>-<env>`, with required
   reviewers and the deployment branch `main` only, holding the secrets
   `SCW_ACCESS_KEY` / `SCW_SECRET_KEY` (an API key of the `ci-ansible`
   application: `scw iam api-key create application-id=<id>`) and the variables
   `SCW_DEFAULT_PROJECT_ID` / `SCW_DEFAULT_ORGANIZATION_ID`.
4. Keep *Actions → Fork pull request workflows* on "require approval for all
   outside collaborators": this repository is public, and only
   `workflow_dispatch` workflows may target the runner.

Turning `ops_runner_enabled` off and re-running `--tags ops_runner` removes the
runner, the wrapper, the key, and the `ansible-ops` user of every instance.
The workstation keeps working as before (same playbooks, `inventory/hosts.yml`).

`tests/test_ops_runner.py` (wrapper, pull request script, workflow guards and
CI inventories) and `tests/ops_runner_test.yml` (sudoers, unit) run in the CI
job `ops-runner`.

## Usage

Clone `Deploiment` next to this repo (`Devops/ansible` + `Devops/Deploiment`):
phase 4 writes each instance's `secrets.yaml` there.

The real inventory is local (MAIR-708): this repository is public and must not
publish the machines' addresses, so `inventory/hosts.yml` is gitignored. It is
generated from Terraform (MAIR-637): the public addresses come from the
`ansible_hosts` output of [`mairie360/Infra`](https://github.com/mairie360/Infra)
(the machines are imported there), everything else about a host (`wg_ip`,
`env_name`, `instance_domain`, `ingress_controller`) from the tracked
`inventory/ci-<org>.yml`:

```bash
# in Devops/Infra (scw CLI profile of the project)
scripts/tf.sh orgs/mairie360 output -json ansible_hosts > /tmp/hosts.json
# here: writes (or updates) the groups mairie360_argocd / mairie360_instances
scripts/inventory-from-terraform.py --org mairie360 /tmp/hosts.json && rm /tmp/hosts.json
```

Re-run it after any machine change instead of editing those groups. It keeps the
other groups, the top-level `vars` and extra per-host keys already in
`hosts.yml`, drops hosts Terraform no longer has, and writes nothing when
Terraform and `inventory/ci-<org>.yml` do not list the same hosts. A new host is
added to `inventory/ci-<org>.yml` (and `orgs/<org>/` of Infra) first.

Existing checkout that still tracks `inventory/hosts.yml`: pulling the MAIR-708 commit
deletes it. Save it first, then put it back (it is ignored by git afterwards):

```bash
cp inventory/hosts.yml /tmp/hosts.yml && git pull && mv /tmp/hosts.yml inventory/hosts.yml
# Already pulled: restore it from the history
git show "$(git log -1 --format=%H --diff-filter=D -- inventory/hosts.yml)^:inventory/hosts.yml" > inventory/hosts.yml
```

**Without access to the Terraform state** (MAIR-800), Scaleway knows the public address
of each machine, so rebuild `inventory/hosts.yml` from `inventory/hosts.example.yml` and
those addresses instead of asking around or digging in the git history:

```bash
scw init                                     # once: a Scaleway key with access to the project
python3 scripts/restore-inventory.py         # writes inventory/hosts.yml
python3 scripts/restore-inventory.py --check # compares an existing one with Scaleway
python3 scripts/restore-inventory.py --force # replaces it (a machine got a new IP)
```

Each host is matched with the Scaleway server of the same name, or with its
`scw_server` variable in the example (`mairie360-argocd` is the server
`argocd hub`), and gets that server's public IPv4. The script refuses a path git
tracks or does not ignore, never prints an address, and fails naming any host
without a server. Everything else (groups, `wg_ip`, `env_name`, domains,
`ingress_controller`) comes from the example: **change the example, then
re-run the script**, never the generated file alone. A client whose machines are
not in this Scaleway project needs its addresses written by hand. The Terraform route above stays the reference for the groups
`mairie360/Infra` manages. `tests/restore_inventory_test.sh` (CI job
`restore-inventory`) runs it against a fake `scw`.

`python3 tests/check_no_public_ip.py` (also run by CI) fails when a tracked file
holds a public IPv4 address; private and documentation ranges are allowed.
CI also runs Gitleaks on every new commit (MAIR-677): a token, a private key
or a long random password fails the check of the push or pull request. Short
or dictionary passwords (`db_password: hunter2`) are not detected, so never
rely on it: encrypt every secret with Ansible Vault (its ciphertext is
allowed). If a secret was pushed, rotate it first. The repository has no
`.gitleaks.toml` or `.gitleaksignore` and CI fails if one appears: either
would let a commit silence the scan.

```bash
ansible-galaxy collection install -r requirements.yml

# Deploy everything (asks for the secrets it cannot find)
ansible-playbook playbooks/site.yml

# One group (adding a client, for instance). Always whole groups: the
# WireGuard phase needs every machine of the group and fails otherwise.
ansible-playbook playbooks/site.yml --limit 'client_example_argocd:client_example_instances'

# Acceptance test (runs Deploiment/scripts/verify.sh from the Argo CD machine)
ansible-playbook playbooks/verify.yml

# One phase or a few: each play of site.yml carries its phase tag
# (phase1, phase1b, phase2, phase3, phase4, phase5)
ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' --tags phase1,phase1b,phase2
```

Rebuilding a lost machine (disaster recovery) does **not** run `site.yml` in one go:
the old sealing key goes back on the machine between phases 2 and 3, and phase 4
is skipped. Follow the runbook in `Deploiment`,
[`docs/en/disaster-recovery.md`](https://github.com/mairie360/Deploiment/blob/main/docs/en/disaster-recovery.md)
([French](https://github.com/mairie360/Deploiment/blob/main/docs/fr/disaster-recovery.md)).

Then commit and push the generated secrets, which Argo CD deploys from git:

```bash
cd ../Deploiment
git add clusters/<org>/instances/<env>/secrets.yaml && git commit && git push
```

Until that file is pushed, the pods stay in `CreateContainerConfigError`:
expected, not a bug.

### Stuck sync operation

The instances ApplicationSet (`roles/k8s_argocd/templates/instances-appset.yaml.j2`)
is single-source on purpose: chart and values are read from the same
`Deploiment` revision, through a path relative to the chart. The former
multi-source layout (chart + `ref: values`) broke whenever a commit landed on
`main` during a sync (`cannot reference a different revision of the same
repository`), and the operation stayed pinned on the old SHA
(argoproj/argo-cd#29716). Do not go back to it.

`playbooks/verify.yml` fails if an operation is still stuck (ComparisonError,
or `Running` for more than `verify_stuck_sync_minutes`, 15 by default). To
unblock it, on the Argo CD machine (the kubeconfig is written by phase 3, with
`argocd` as its default namespace):

```bash
sudo KUBECONFIG=/root/.kube/argocd-cli.yaml argocd app terminate-op <env> --core
```

A group whose Applications are still multi-source (provisioned before
MAIR-173) switches to the single-source layout on the next `site.yml` run,
all its instances at once (the ApplicationSet is per group). Check it with:

```bash
# On the Argo CD machine: every instance Application is single-source
sudo kubectl -n argocd get applications.argoproj.io \
  -o custom-columns='NAME:.metadata.name,SOURCE:.spec.source.path,SOURCES:.spec.sources[*].ref'

ansible-playbook playbooks/verify.yml
```

Acceptance test: push a commit to `Deploiment` `main` while an instance is
syncing, and check that it converges on the new commit on its own.

### Sync policy of the instances (MAIR-801)

`deploiment_auto_sync_envs` (`inventory/group_vars/all.yml`, `dev` and
`staging`) lists the environments whose Application syncs by itself (prune +
self-heal); the others, prod included, are synced by hand. The list is
rendered into the ApplicationSet's `templatePatch`, so it only reaches the
cluster when phase 2 re-applies the ApplicationSet.

On 2026-11-06 the staging Application had no automated sync (last auto sync
2026-10-20) and showed OutOfSync / Degraded: `staging` had been added to the
list by MAIR-444 (2026-11-02), but the ApplicationSet on the machine was the
one applied before. Every Promote to staging then needed a hand sync.
`playbooks/verify.yml` now compares every instance Application with the list
and fails with the command to run; `tests/image_updater_test.yml` checks that
the rendered `templatePatch` automates exactly the list. After changing it:

```bash
ansible-playbook playbooks/site.yml --limit mairie360_argocd --tags phase2
ansible-playbook playbooks/verify.yml --limit mairie360_argocd
# On the Argo CD machine: which Applications sync by themselves
sudo kubectl -n argocd get applications -o custom-columns=NAME:.metadata.name,AUTO:.spec.syncPolicy.automated,SYNC:.status.sync.status,HEALTH:.status.health.status
```

An Application that stays Degraded once synced is a workload problem, not
this one: look at its resources (`argocd app get <env> --core` with the
kubeconfig above, then the pods and events of `mairie360-<env>`).

## Image tags (MAIR-186, MAIR-444)

What runs on an instance is always what `Deploiment` says: argocd-image-updater
never writes into the Applications any more, and the instances ApplicationSet
no longer preserves their Helm parameters, so a parameter set by hand is
reverted and shown as drift.

| Environment | Who moves the image tags | Where |
|---|---|---|
| dev | argocd-image-updater, newest `dev-<sha>` | a `chore(deps)` PR on `main` editing `clusters/<org>/instances/dev/values.yaml`, auto-merged by Deploiment's `image-updater-automerge` workflow once CI passes |
| staging | argocd-image-updater, newest `staging-<sha>` | a commit on the `staging` branch into `clusters/<org>/instances/staging/images.yaml` (read after `values.yaml`); staging syncs by itself |
| prod, clients | nobody: no `ImageUpdater` | the tags pinned in `clusters/_base/prod.yaml`, moved by a reviewed commit and a `Promote` |

Every service is tracked, `database` and `liquibase-migrations` included. The
policies live in `image_updater_policies` (`roles/k8s_argocd/defaults/main.yml`).
The write-back needs a fine-grained token on `mairie360/Deploiment` (contents
and pull requests read/write): `DEPLOIMENT_GIT_USER` / `DEPLOIMENT_GIT_TOKEN`,
else the `argocd/deploiment-git-creds` already on the Argo CD machine, else a
prompt.

**Switching a group that ran the former image-updater.** Phase 2 deletes the
`ImageUpdater` of every environment no longer followed (prod), then lists the
tags it left in the Applications' Helm parameters and **stops** while any is
left: dropping them silently would roll the instance back to its pinned tags.

```bash
# 1. First run: the play stops and prints, per Application, the tags written outside git.
ansible-playbook playbooks/site.yml --limit mairie360_argocd:mairie360_instances
# 2. Pin the prod tags to keep in Deploiment clusters/_base/prod.yaml (PR + Promote).
#    dev and staging need nothing: the git write-back commits the newest tags on its next pass.
# 3. Remove the leftover parameters; the ApplicationSet then follows git only.
ansible-playbook playbooks/site.yml --limit mairie360_argocd:mairie360_instances \
  -e image_updater_clear_parameters=true
```

## Secrets

Phase 4 (`playbooks/secrets.yml`, also imported by `site.yml`) runs
`Deploiment/scripts/seal-secrets.sh` **on the Argo CD machine**: it is the only
one that reaches the instance API server, through the tunnel. For each instance:

- it waits for the sealed-secrets controller Argo CD deploys on it;
- generated secrets (`JWT_SECRET`, Postgres, Redis ACL) are generated by
  `seal-secrets.sh` on the first run, then kept. Nothing is ever rotated here;
- `RESTIC_PASSWORD` and the admin account's first `ADMIN_PASSWORD` are
  resolved by the role itself (MAIR-599) and **stored in the team vault**,
  Scaleway Secret Manager (MAIR-617), see [The vault of the instance secrets](#the-vault-of-the-instance-secrets);
- external secrets are resolved in this order: `instance_secrets[<host>]`
  (e.g. `-e @secrets.yml --ask-vault-pass`, see
  `roles/k8s_instance_secrets/defaults/main.yml`), environment variables
  (`RESEND_API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `COCKPIT_TOKEN`), then, for the Resend key and the
  Cockpit token, the vault (secrets `resend-api-key` and `cockpit-token`,
  MAIR-803: a value passed or typed once is stored there, so a later run, the
  CI included, needs nothing typed), and for the S3 keys the per-bucket keys
  of the vault (secrets `backup-s3-key` and `elearning-s3-key`, MAIR-619: each
  key only reaches its own bucket; issued by `scripts/issue-bucket-keys.sh` of
  `mairie360/Infra`, never written by this role), otherwise the value already on the
  instance is kept, else **a prompt** (empty to skip). The Resend key is **mandatory at the
  first sealing** of an instance (no `<env>-keycloak-secret` on it yet, MAIR-599):
  Keycloak imports its SMTP settings once, an empty key would leave its e-mails
  broken for good, so the play fails instead. The backup bucket keys are only asked
  when `backup.enabled` is true in the instance values, and the Scaleway
  Cockpit token (`COCKPIT_TOKEN`, MAIR-131, read by the OpenTelemetry
  Collector) when `global.observability.enabled` is;
- the town hall administrator's e-mail (`instance_secrets[<host>].admin_email`
  / `ADMIN_EMAIL`, MAIR-170) is resolved the same way (the one already sealed
  on the instance instead of the vault, MAIR-803), but is **mandatory**:
  prompted in clear (not hidden), and the play **fails** if it is still empty
  — no instance should keep Database's public template admin account by
  accident. The matching first `ADMIN_PASSWORD` is generated by the role (see
  below);
- GHCR credentials come from `GHCR_USER` / `GHCR_TOKEN`, else from
  `argocd/ghcr-secret` (phase 2 prompts for it once per group); the Deploiment
  write-back token the same way, from `DEPLOIMENT_GIT_USER` /
  `DEPLOIMENT_GIT_TOKEN`, else `argocd/deploiment-git-creds`;
- `secrets.yaml` is copied into the local `Deploiment` checkout, and every
  sealing key of the instance is stored in the vault: without them, a
  reinstall makes every committed `secrets.yaml` of the instance undecryptable.

In CI, pass `-e secrets_prompt=false`: missing secrets are then only reported
(except the admin e-mail, and the Resend key at the first sealing, which fail).

### The vault of the instance secrets

Three values exist nowhere but on the instance, and the instance alone cannot
give them back after a loss. The role keeps them in **Scaleway Secret Manager**
(MAIR-617), one secret per value under the path `/<org>/<env>`, in the Scaleway
project of the group (`vault_project_id`, see below):

| Secret | Holds | Lost without it |
| --- | --- | --- |
| `restic-password` | `RESTIC_PASSWORD` of the backup bucket | every backup, if the instance or its sealing key is lost |
| `admin-password` | plaintext first password of the town hall admin account (MAIR-170) | the first login: the instance only holds its argon2id hash (MAIR-414) |
| `sealing-keys` | one version per sealing key of the instance: a single one, the controller's renewal is off (Deploiment `keyrenewperiod: "0"`) | every committed `secrets.yaml`, after a reinstall |

**The role never creates a secret.** Terraform does, from
[`mairie360/Infra`](https://github.com/mairie360/Infra) (`orgs/<org>`,
protected secrets that cannot be deleted by mistake); the role only looks them
up, adds versions and reads them. When one is missing the play stops before
sealing anything and asks for `terraform apply`. Nothing secret is written on
the operator's machine any more.

The disaster-recovery runbook of `Deploiment` (`docs/en/disaster-recovery.md`) needs the sealing
key file and `RESTIC_PASSWORD` to rebuild a lost machine: without the sealing key, every committed
`secrets.yaml` of the instance has to be sealed again; without `RESTIC_PASSWORD`, the backups
cannot be read at all. `scripts/restore-sealing-key-file.sh <key file> <ssh target of the instance>`
puts the sealing keys of a key file back on a rebuilt machine, between phases 2 and 3 (it
restarts the controller if it already runs).

Secret Manager keeps **at most 20 versions per secret** (disabled ones
included). Before adding versions the role counts them and, when one secret
would go past `vault_max_versions` (20), stops without writing anything and
names it: delete versions no longer needed (`scw secret version delete`, never
the current value nor a sealing key still in use) or raise the quota through
Scaleway support. Archived copies of MAIR-599 and deliberate key rotations are
the only things that add versions after the first run.

On every run, per generated value: what is already sealed on the instance wins,
then the latest version in the vault, and only then a new hex value is
generated. A value sealed on the instance is **never** replaced (a new
`RESTIC_PASSWORD` would make the bucket unreadable), not even by
`seal_extra_args=--rotate`. The role adds the value as a new version **and
reads it back** before passing it to `seal-secrets.sh` (`RESTIC_PASSWORD`,
`ADMIN_PASSWORD`), so the sealed value is always the stored one, also on a
re-run before the first `secrets.yaml` is pushed. For the admin account,
`seal-secrets.sh` seals only the hash; once the instance holds the hash, the
role passes it back unchanged and adds no version. After sealing, each sealing
key of the instance not stored yet is added as a version whose description is
the key's name (`sealed-secrets-keyxxxxx`).

**Credentials**, read on the controller: `SCW_SECRET_KEY` and
`SCW_DEFAULT_PROJECT_ID` when set, else the scw CLI profile (`SCW_PROFILE`,
else the active profile of `~/.config/scw/config.yaml`, `SCW_CONFIG_PATH` to
use another file); `vault_project_id` in the inventory wins over both for the
project. The API key needs `SecretManagerFullAccess` on that project. Without
any credentials the play stops and asks for `scw init`.

```bash
# Read a value (latest enabled version), e.g. the first admin password of dev.
# Only right after a complete run: a stopped run can leave an archive enabled
# until the next one (check `scw secret version list <secret-id>`, archives
# are described "MAIR-617 archived ...").
scw secret secret list path=/mairie360/dev name=admin-password -o json | jq -r '.[0].id'
scw secret version access <secret-id> revision=latest_enabled raw=true

# Restore the sealing keys of a reinstalled instance, BEFORE Argo CD syncs its
# sealed-secrets controller (it would generate a new key otherwise). From the
# workstation; kubectl runs on the group's Argo CD machine over SSH, with
# /root/.kube/instance-<env>.yaml. Stops at the first failure.
scripts/restore-sealing-keys.sh mairie360 dev root@<argocd machine>
```

**Migration from MAIR-599.** Runs before MAIR-617 kept the values in
`~/.mairie360` on the operator's machine (`restic-<org>-<env>.txt`,
`admin-<org>-<env>.txt`, `sealing-keys/<org>-<env>.yaml`, with `.from-<host>`
and `.<pid>.<date>~` older copies), and runs before MAIR-599 left admin
plaintexts on the Argo CD machine (`/root/.mairie360/admin-<org>-<env>.txt*`).
The role finds the files of its own instance in both places, adds each
distinct older value as an archived version (description `MAIR-617 archived
<origin>:<file>`, disabled right after), then the current value when it
changes, reads them back,
and only then deletes the files. A run stopped between writing an archive and
disabling it leaves that archive as `latest_enabled`; the next run disables it
(the role itself never reads `latest_enabled`). It runs once per instance:
`playbooks/secrets.yml --limit <host>` changes no sealed value; commit the
resulting `secrets.yaml` as usual (its ciphertext changes on every run, its
values do not).

```bash
# Change a single instance's Resend key
RESEND_API_KEY=re_xxx ansible-playbook playbooks/secrets.yml --limit mairie360-dev

# Seal the Cockpit token before enabling global.observability (no prompt then)
COCKPIT_TOKEN=xxx ansible-playbook playbooks/secrets.yml --limit mairie360-dev
```

### Secrets never reach the logs

Every task that reads, holds or writes a secret (passwords, tokens, WireGuard
keys, kubeconfigs, the sealing key, hidden prompts) carries `no_log: true`, and
no task prints one. The Argo CD admin password in particular is not printed:
phase 2 only shows the command that reads it on the Argo CD machine:

```bash
sudo kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

Change it after the first login (`argocd account update-password`), then
delete `argocd-initial-admin-secret`. Run logs (`logs/`, `*.log`) are
git-ignored. Before committing a role change, run the static check:

```bash
python3 tests/check_no_log.py       # fails on a secret-handling task without no_log
python3 tests/check_gdpr_config.py  # fails when Hubble metrics are labelled with IP addresses
ansible-playbook tests/gdpr_gaps_test.yml   # the GDPR gaps of verify.yml, on localhost
ansible-playbook tests/image_updater_test.yml   # no ImageUpdater in prod, git write-back for dev/staging
ansible-playbook tests/seal_passwords_test.yml  # MAIR-599: generated secrets resolution, Resend key at the first sealing
python3 -m unittest tests/test_vault_filters.py  # MAIR-617: what is written to the vault
bash tests/restore_sealing_keys_test.sh          # MAIR-617: sealing keys restore (scw, ssh stubbed)
ansible-playbook tests/vault_test.yml           # MAIR-617: the vault tasks against a local stand-in of Secret Manager
python3 tests/check_image_updater_services.py ../Deploiment  # image_updater_services = services deployed by Deploiment
ansible-lint                                    # pre-existing violations: .ansible-lint, .ansible-lint-ignore
bash tests/mair_677_secret_scan_test.sh   # Gitleaks blocks tokens and keys, not vault ciphertext (docker)
```

## GDPR checks of the machines (MAIR-293)

`playbooks/verify.yml` ends with a read-only GDPR play on every machine (Argo CD and instances).
It collects auditd, the effective sshd settings (`sshd -T`: no password, no root), the UFW rule of
port 22 (open only on `wg0`), `k3s secrets-encrypt status`, unattended-upgrades, an encrypted
(`crypt`) volume on the machines marked `hosted_at_mairie: true`, journald's `MaxRetentionSec`
against the `technical_logs` period of Devops/Deploiment `compliance/<org_id>/retention.yaml`, and
the authorized SSH keys, WireGuard peers and Argo CD accounts against `compliance/<org_id>/access.yaml`
(read from the Deploiment checkout of the Argo CD machine; an empty `access.yaml` is reported as
"no decision"). Each failed check is a gap (`playbooks/templates/gdpr_gaps.j2`); the play fails on any
gap not listed in `gdpr_allowed_gaps` (group_vars, `id: reason`). The role `k8s_node` installs auditd
and bounds journald to `technical_logs_retention` (1 year by default), and the Hubble metrics no
longer carry IP addresses.

## Adding a client

Three files, no playbook or role to touch:

1. `inventory/hosts.yml` (local, gitignored): the `client_x_argocd` and `client_x_instances` groups;
   also add the pattern to `inventory/hosts.example.yml` with documentation addresses
2. `inventory/group_vars/client_x_argocd.yml` and `inventory/group_vars/client_x_instances.yml`:
   one line, `org_id: client-x`
3. in `Deploiment`: `clusters/client-x/instances/prod/values.yaml`

## Network observability (Cilium / Hubble)

Every machine runs Cilium as CNI instead of flannel (`k8s_node`,
`tasks/cilium.yml`). Cilium enforces the `NetworkPolicy` objects of the
`Deploiment` chart, and Hubble records every flow between fronts, BFFs, APIs,
Postgres and Redis with its verdict (`FORWARDED` / `DROPPED`). It is installed
by Ansible, not by Argo CD: without a CNI no pod starts, so nothing would ever
sync.

On a machine (as root, through the tunnel):

```bash
cilium status                                                  # agent, operator, Hubble
hubble observe -n mairie360-dev --verdict DROPPED --last 50    # what the policies refuse
hubble observe -n mairie360-dev --protocol http --last 50      # fronts -> bffs -> apis requests
cilium hubble ui                                               # service map, http://localhost:12000
```

From the workstation, `scripts/hubble-flows.sh` in `Deploiment` prints the
same thing hop by hop through a `kubectl port-forward`. HTTP details (method,
path, status) need `global.networkPolicy.ciliumL7Visibility` in the instance
values.

Migrating a machine provisioned with flannel: re-run `site.yml`. k3s restarts
with flannel disabled, Cilium is installed, every pod is restarted once onto
Cilium (a few minutes of downtime) and the flannel interfaces are removed.
Do `dev` first. Pinned versions: `cilium_version`, `cilium_cli_version`,
`hubble_cli_version` in `inventory/group_vars/all.yml` (see "Upgrading k3s and
Argo CD" for `k3s_version` / `argocd_version`).

## Upgrading k3s and Argo CD

Versions are pinned in `inventory/group_vars/all.yml` and bumped through a PR:

| Variable | Pin | Why this one |
|---|---|---|
| `k3s_version` | `v1.35.8+k3s1` | Kubernetes 1.35 is supported upstream until 2027-02-28 and is the only minor inside every matrix of the stack: Cilium 1.20 (1.33-1.36), Argo CD 3.4 (1.32-1.35), cert-manager 1.21 (1.33-1.36), ingress-nginx controller 1.15 (1.31-1.35) and Traefik 3.7 (>= 1.25), all deployed by `Deploiment` |
| `argocd_version` | `v3.4.9` | Supported 3.x line. 3.5 moves to Helm 4 and is left for a later bump |

**k3s.** `k8s_node` installs the exact `k3s_version` with the `install.sh` of
that tag (the installer checks the binary's sha256). On every run it compares
`k3s --version` with the pin and re-runs the installer only when they differ,
**one machine at a time** (`throttle: 1`): each machine is a single-node
cluster, so an upgrade restarts all its pods for a minute or two.

Kubernetes never downgrades and upgrades **one minor at a time**: the role
refuses a pin more than one minor above the installed version. From an older
machine, go through every minor, latest patch of each (`update.k3s.io/v1-release/channels`
lists them), whole groups, `dev` first:

```bash
# e.g. from v1.31: 1.32 -> 1.33 -> 1.34 -> 1.35 (the pin)
for v in v1.32.13+k3s1 v1.33.13+k3s2 v1.34.13+k3s1; do
  ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' -e k3s_version=$v || break
done
ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances'
ansible-playbook playbooks/verify.yml
```

**One machine at a time, with checks.** The loop above upgrades a whole group
per step. The k3s phase alone does not need the whole group (only the WireGuard
phase does), so the four `mairie360` machines (dev, staging, Argo CD, prod) went
from v1.31.14 to v1.35.8 one machine at a time, one minor at a time:

```bash
ansible-playbook playbooks/site.yml --limit mairie360-dev --tags phase1 -e k3s_version=v1.32.13+k3s1
```

After each step, before the next one: the node is `Ready`, no pod is outside
`Running` / `Completed`, every Application of the group's Argo CD is
`Synced` / `Healthy`, a front answers (`https://login.<instance_domain>/`) and
the free disk space is still above the limit below. A second run of the same
command must report `changed=0`. Stop at the first anomaly. Argo CD is unaffected
while an instance is upgraded; when its own machine restarts, Argo CD is
unreachable for a minute or two and the deployed applications keep running.
A dry run (`--check --diff --tags phase1,phase1b`) reports `changed=0` on a
machine that already runs the pin.

**Disk space.** The root volumes are small (17 GB on the Argo CD machine, 26 GB on
the others). Each minor adds about 250 MB (a new directory in
`/var/lib/rancher/k3s/data`) plus new system images, and container images of
old releases pile up until the kubelet cleans them at 85 % usage. Run
`crictl rmi --prune` (removes only images no container uses) before every step
and stop below 3 GB free (1.7 GB on the 17 GB volume): the kubelet evicts pods at
10 % free space.

**Argo CD.** Phase 2 re-applies `install.yaml` of `argocd_version` with
server-side apply (required since 3.3) and replaces the `argocd` CLI when its
checksum differs from the pinned release. Before crossing a minor, read the
upstream notes (`docs/operator-manual/upgrading/` in `argoproj/argo-cd`). The
2.13 -> 3.4 jump changes, among others: annotation-based resource tracking by
default (every Application re-syncs once and resources get the
`argocd.argoproj.io/tracking-id` annotation), `logs, get` RBAC enforced,
repositories no longer read from `argocd-cm`, more resources excluded by
default (`Endpoints`, `Lease`, `CiliumIdentity`, `CiliumEndpoint`; not
`CiliumNetworkPolicy`). None of those is used by
`Deploiment` today.

## Conventions that matter

**`org_id` keeps its dashes** (`client-example`) because it must match the
`clusters/<org_id>/` folder of `Deploiment`. Ansible group names only accept
underscores. `org_group` (defined in `inventory/group_vars/all.yml`) is the
bridge: use it in `groups[...]`, never `org_id`.

**`env_name` is the name of the cluster in Argo CD.** The ApplicationSet targets
`destination.name: '{{ .path.basename }}'`, hence the name of the
`clusters/<org>/instances/<env>/` folder. Registering it under another name (such
as `inventory_hostname`) leaves the Application stuck on "cluster does not
exist", with no other symptom. The `k8s_instance_link` role enforces the right name.

**The `mairie360.fr/role=instance` label** tells an instance from the Argo CD
machine. The bootstrap AppSets select it: without it, no bootstrap layer is
deployed on the instance.

**`ansible_host` is for SSH, `wg_ip` is for Argo CD.** Port 6443 gives
administrator access: it is only open on `wg0`, and only to the group's
Argo CD tunnel IP, never to the Internet.

## Closing the public SSH

`k8s_node` leaves port 22 open on purpose: cutting the public SSH before the
tunnel is validated would lock the machine out. Once the connection through the
WireGuard IP is confirmed:

```bash
ansible-playbook playbooks/site.yml -e wireguard_close_public_ssh=true
```

The role checks that the tunnel answers before removing the rule.
