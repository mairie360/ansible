# ansible

Provisionnement des machines Mairie360. Ce dépôt **prépare les serveurs et
amorce Kubernetes** ; l'état désiré des applications vit dans
[`mairie360/Deploiment`](https://github.com/mairie360/Deploiment).

## Modèle

**Un Argo CD et une instance Mairie360 par machine.** Un *groupe* = un Argo CD
+ ses instances.

```
Groupe mairie360 (4 machines)      Groupe client-x (2 machines)
  mairie360-argocd                   client-x-argocd
  mairie360-dev                      client-x-prod
  mairie360-staging
  mairie360-prod
```

Chaque Argo CD ne pilote que les instances de son groupe, et son tunnel
WireGuard ne porte que sur ce groupe. Un client compromis n'a aucune route
réseau vers un autre : l'isolation vient de la topologie.

## The five phases

| Phase | Role | Target | What it does |
|---|---|---|---|
| 1 | `k8s_node` | all | Hardening, firewall (6443 only from the group's Argo CD, over `wg0`), k3s (Traefik disabled, ServiceLB on), Cilium CNI + Hubble |
| 1b | `wireguard` | all | Group tunnel: the Argo CD machine listens, the instances connect to it |
| 2 | `k8s_argocd` | `*_argocd` | Argo CD + `platform-app.yaml` + the group's ApplicationSet, `argocd/ghcr-secret`, one `ImageUpdater` per instance |
| 3 | `k8s_instance_link` | `*_instances` | `argocd cluster add` on the WireGuard IP + labels (`mairie360.fr/role`, `org`, `env`, `ingress`) + annotation `mairie360.fr/revision` |
| 4 | `k8s_instance_secrets` | `*_instances` | Seals the instance secrets with `seal-secrets.sh`, asks for the missing external ones, copies `secrets.yaml` back to the local `Deploiment` checkout |
| 5 | `github_runner` | `*_argocd` with `github_runner_enabled` | Ephemeral GitHub Actions runner of `Deploiment` and a least-privilege kubeconfig on staging, so the Promote workflow runs `verify.sh` before prod (MAIR-346) |

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
# 1. inventory/hosts.yml, on the instance host (dev first):
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
prod, fast-forward only). That map is the single source for both layers:

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

### Rollout of MAIR-346

An annotated cluster reads `bootstrap/addons/` of `Deploiment` at its own
branch, and that directory only exists on a branch once the MAIR-346 change
of `Deploiment` has reached it. Annotating `prod` before that would point
its bootstrap Applications at a path that does not exist yet. Order:

1. Merge the `Deploiment` change on `main` (dev's bootstrap is unchanged: it
   follows `main` either way).
2. Promote `Deploiment` to `staging` (Promote workflow, `target=staging`) and
   sync staging in Argo CD.
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
and never opens anything:

| | |
|---|---|
| Registration | Repository level on `mairie360/Deploiment`, labels `self-hosted`, `linux`, `mairie360-argocd-<org_id>`. A workflow targets it with `runs-on: [self-hosted, linux, mairie360-argocd-mairie360]` |
| Ephemeral | One just-in-time (JIT) registration per job: `gh-runner.service` registers, runs one job, exits and systemd restarts it. Before every job the runner directory is re-copied from the pinned release and its `HOME` (`/var/lib/gh-runner/home`) emptied, so nothing a job writes survives |
| User | `gh-runner`, non-root system user, no login shell, cannot write to `/home/gh-runner`. The systemd unit is sandboxed (`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, no access to `/root` nor `/etc/gh-runner`) |
| Network | Outbound HTTPS to GitHub only: no inbound port, no firewall or WireGuard change. The unit's `IPAddressDeny` blocks the tunnel subnet and this machine's pod and Service networks (Argo CD), except the WireGuard IPs of the verified instances |
| Instance access | For each env of `github_runner_verify_envs` (default `staging`): ServiceAccount `mairie360-ci/verify-runner` with exactly the rules listed at the top of `Deploiment/scripts/verify.sh` (no Secret read, no exec; mapped to its steps in `roles/github_runner/templates/verify-rbac.yaml.j2`), plus a `ValidatingAdmissionPolicy` that only lets it create, attach to and delete `verify-probe-*` pods of image `curlimages/curl:8.10.1` with no volume, env nor token. Kubeconfig: `/home/gh-runner/.kube/instance-<env>.yaml` (root:gh-runner, 0440, one context `<env>`, current). The admin kubeconfigs of `/root/.kube/` are never readable by the runner |
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

`playbooks/verify.yml` runs `verify.sh` on those environments **as
`gh-runner` with its kubeconfig**, like the Promote workflow does, and fails
when `gh-runner.service` is not active. On the machine:
`journalctl -u gh-runner` (one "registered ephemeral runner" line per job).

## Usage

Clone `Deploiment` next to this repo (`Devops/ansible` + `Devops/Deploiment`):
phase 4 writes each instance's `secrets.yaml` there.

```bash
ansible-galaxy collection install -r requirements.yml

# Deploy everything (asks for the secrets it cannot find)
ansible-playbook playbooks/site.yml

# One group (adding a client, for instance). Always whole groups: the
# WireGuard phase needs every machine of the group and fails otherwise.
ansible-playbook playbooks/site.yml --limit 'client_example_argocd:client_example_instances'

# Acceptance test (runs Deploiment/scripts/verify.sh from the Argo CD machine)
ansible-playbook playbooks/verify.yml
```

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

Migrating a group whose Applications are still multi-source (every group
provisioned before MAIR-173): re-run `site.yml` on the whole group. The
ApplicationSet is per group, so all its instances switch at once (there is no
"dev first" here). Phase 2 carries the image tags written by
argocd-image-updater over to the new layout, then:

```bash
# On the Argo CD machine: every instance Application is single-source...
sudo kubectl -n argocd get applications.argoproj.io \
  -o custom-columns='NAME:.metadata.name,SOURCE:.spec.source.path,SOURCES:.spec.sources[*].ref'
# ...and still carries its image tags (not the values.yaml ones)
sudo kubectl -n argocd get applications.argoproj.io <env> -o jsonpath='{.spec.source.helm.parameters}'

ansible-playbook playbooks/verify.yml
```

Acceptance test: push a commit to `Deploiment` `main` while an instance is
syncing, and check that it converges on the new commit on its own.

## Secrets

Phase 4 (`playbooks/secrets.yml`, also imported by `site.yml`) runs
`Deploiment/scripts/seal-secrets.sh` **on the Argo CD machine**: it is the only
one that reaches the instance API server, through the tunnel. For each instance:

- it waits for the sealed-secrets controller Argo CD deploys on it;
- generated secrets (`JWT_SECRET`, Postgres, Redis ACL, `RESTIC_PASSWORD`) are
  generated on the first run, then kept. Nothing is ever rotated here;
- external secrets are resolved in this order: `instance_secrets[<host>]`
  (e.g. `-e @secrets.yml --ask-vault-pass`, see
  `roles/k8s_instance_secrets/defaults/main.yml`), environment variables
  (`RESEND_API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `COCKPIT_TOKEN`), the value already on the instance,
  otherwise **a prompt** (empty to skip). The backup bucket keys are only asked
  when `backup.enabled` is true in the instance values, and the Scaleway
  Cockpit token (`COCKPIT_TOKEN`, MAIR-131, read by the OpenTelemetry
  Collector) when `global.observability.enabled` is;
- the town hall administrator's e-mail (`instance_secrets[<host>].admin_email`
  / `ADMIN_EMAIL`, MAIR-170) is resolved the same way, but is **mandatory**:
  prompted in clear (not hidden), and the play **fails** if it is still empty
  — no instance should keep Database's public template admin account by
  accident. `seal-secrets.sh` generates the matching `ADMIN_PASSWORD` itself
  and keeps it on later runs; this role cannot yet read that value back to
  deposit it on the workstation (tracked in `roles/k8s_instance_secrets/tasks/main.yml`);
- GHCR credentials come from `GHCR_USER` / `GHCR_TOKEN`, else from
  `argocd/ghcr-secret` (phase 2 prompts for it once per group);
- `secrets.yaml` is copied into the local `Deploiment` checkout, and the
  instance sealing key into `~/.mairie360/sealing-keys/<org>-<env>.yaml`.
  Store that key in the team vault: without it, a reinstall makes every
  committed `secrets.yaml` of the instance undecryptable.

In CI, pass `-e secrets_prompt=false`: missing secrets are then only reported.

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
python3 tests/check_no_log.py   # fails on a secret-handling task without no_log
```

## Ajouter un client

Trois fichiers, aucun playbook ni rôle à toucher :

1. `inventory/hosts.yml` — les groupes `client_x_argocd` et `client_x_instances`
2. `inventory/group_vars/client_x_argocd.yml` et `inventory/group_vars/client_x_instances.yml` —
   une ligne : `org_id: client-x`
3. dans `Deploiment` : `clusters/client-x/instances/prod/values.yaml`

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
for v in v1.32.13+k3s1 v1.33.13+k3s2 v1.34.11+k3s1; do
  ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances' -e k3s_version=$v || break
done
ansible-playbook playbooks/site.yml --limit 'mairie360_argocd:mairie360_instances'
ansible-playbook playbooks/verify.yml
```

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

## Conventions qui comptent

**`org_id` garde les tirets** (`client-example`) parce qu'il doit correspondre
au dossier `clusters/<org_id>/` de `Deploiment`. Les noms de groupes Ansible
n'acceptent que des underscores. `org_group` (défini dans `inventory/group_vars/all.yml`)
fait le pont — c'est lui qu'il faut utiliser dans `groups[...]`, jamais
`org_id`.

**`env_name` est le nom du cluster dans Argo CD.** L'ApplicationSet cible
`destination.name: '{{ .path.basename }}'`, donc le nom du dossier
`clusters/<org>/instances/<env>/`. Un enregistrement sous un autre nom (comme
`inventory_hostname`) produit une Application bloquée en « cluster does not
exist », sans autre symptôme. Le rôle `k8s_instance_link` force le bon nom.

**Le label `mairie360.fr/role=instance`** distingue une instance de la machine
Argo CD. Les AppSets du socle le sélectionnent : sans lui, aucun socle n'est
déployé sur l'instance.

**`ansible_host` is for SSH, `wg_ip` is for Argo CD.** Port 6443 gives
administrator access: it is only open on `wg0`, and only to the group's
Argo CD tunnel IP, never to the Internet.

## Fermeture du SSH public

`k8s_node` laisse volontairement le port 22 ouvert : couper le SSH public avant
d'avoir validé le tunnel verrouillerait la machine. Une fois la connexion par
l'IP WireGuard confirmée :

```bash
ansible-playbook playbooks/site.yml -e wireguard_close_public_ssh=true
```

Le rôle vérifie que le tunnel répond avant de retirer la règle.
