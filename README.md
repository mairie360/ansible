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

## The four phases

| Phase | Role | Target | What it does |
|---|---|---|---|
| 1 | `k8s_node` | all | Hardening, firewall (6443 only from the group's Argo CD, over `wg0`), k3s (Traefik disabled, ServiceLB on), Cilium CNI + Hubble |
| 1b | `wireguard` | all | Group tunnel: the Argo CD machine listens, the instances connect to it |
| 2 | `k8s_argocd` | `*_argocd` | Argo CD + `platform-app.yaml` + the group's ApplicationSet, `argocd/ghcr-secret`, `argocd/deploiment-git-creds`, one `ImageUpdater` per dev / staging instance |
| 3 | `k8s_instance_link` | `*_instances` | `argocd cluster add` on the WireGuard IP + labels (`mairie360.fr/role`, `org`, `env`, `ingress`) |
| 4 | `k8s_instance_secrets` | `*_instances` | Seals the instance secrets with `seal-secrets.sh`, asks for the missing external ones, copies `secrets.yaml` back to the local `Deploiment` checkout |

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

## Usage

Clone `Deploiment` next to this repo (`Devops/ansible` + `Devops/Deploiment`):
phase 4 writes each instance's `secrets.yaml` there.

The real inventory is local (MAIR-708): this repository is public and must not
publish the machines' addresses, so `inventory/hosts.yml` is gitignored. Create it
from the template and fill in the real addresses (ask the team for the current
file; it will move to a private source later):

```bash
cp inventory/hosts.example.yml inventory/hosts.yml
```

Existing checkout that still tracks `inventory/hosts.yml`: pulling the MAIR-708 commit
deletes it. Save it first, then put it back (it is ignored by git afterwards):

```bash
cp inventory/hosts.yml /tmp/hosts.yml && git pull && mv /tmp/hosts.yml inventory/hosts.yml
# Already pulled: restore it from the history
git show "$(git log -1 --format=%H --diff-filter=D -- inventory/hosts.yml)^:inventory/hosts.yml" > inventory/hosts.yml
```

`python3 tests/check_no_public_ip.py` (also run by CI) fails when a tracked file
holds a public IPv4 address; private and documentation ranges are allowed.

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
  resolved by the role itself and **backed up on the operator's machine**
  (MAIR-599), see [Backups of the generated secrets](#backups-of-the-generated-secrets);
- external secrets are resolved in this order: `instance_secrets[<host>]`
  (e.g. `-e @secrets.yml --ask-vault-pass`, see
  `roles/k8s_instance_secrets/defaults/main.yml`), environment variables
  (`RESEND_API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `COCKPIT_TOKEN`), the value already on the instance,
  otherwise **a prompt** (empty to skip). The Resend key is **mandatory at the
  first sealing** of an instance (no `<env>-keycloak-secret` on it yet, MAIR-599):
  Keycloak imports its SMTP settings once, an empty key would leave its e-mails
  broken for good, so the play fails instead. The backup bucket keys are only asked
  when `backup.enabled` is true in the instance values, and the Scaleway
  Cockpit token (`COCKPIT_TOKEN`, MAIR-131, read by the OpenTelemetry
  Collector) when `global.observability.enabled` is;
- the town hall administrator's e-mail (`instance_secrets[<host>].admin_email`
  / `ADMIN_EMAIL`, MAIR-170) is resolved the same way, but is **mandatory**:
  prompted in clear (not hidden), and the play **fails** if it is still empty
  — no instance should keep Database's public template admin account by
  accident. The matching first `ADMIN_PASSWORD` is generated by the role (see
  below);
- GHCR credentials come from `GHCR_USER` / `GHCR_TOKEN`, else from
  `argocd/ghcr-secret` (phase 2 prompts for it once per group); the Deploiment
  write-back token the same way, from `DEPLOIMENT_GIT_USER` /
  `DEPLOIMENT_GIT_TOKEN`, else `argocd/deploiment-git-creds`;
- `secrets.yaml` is copied into the local `Deploiment` checkout, and the
  instance sealing key into `~/.mairie360/sealing-keys/<org>-<env>.yaml`.
  Store that key in the team vault: without it, a reinstall makes every
  committed `secrets.yaml` of the instance undecryptable.

In CI, pass `-e secrets_prompt=false`: missing secrets are then only reported
(except the admin e-mail, and the Resend key at the first sealing, which fail).

### Backups of the generated secrets

Two generated values exist nowhere but on the instance, so the role keeps a copy
on the operator's machine, in `operator_backup_dir` (default `~/.mairie360`,
mode 700, files mode 600, outside any git repository):

| File | Holds | Lost without it |
| --- | --- | --- |
| `restic-<org>-<env>.txt` | `RESTIC_PASSWORD` of the backup bucket | every backup, if the instance or its sealing key is lost |
| `admin-<org>-<env>.txt` | plaintext first password of the town hall admin account (MAIR-170) | the first login: the instance only holds its argon2id hash (MAIR-414) |
| `sealing-keys/<org>-<env>.yaml` | the instance sealing key | every committed `secrets.yaml`, after a reinstall |

Store the three in the team vault. On every run, per value: what is already
sealed on the instance wins, then the backup file, and only then a new hex value
is generated. A value sealed on the instance is **never** replaced (a new
`RESTIC_PASSWORD` would make the bucket unreadable), not even by
`seal_extra_args=--rotate`. The role passes both values to `seal-secrets.sh`
(`RESTIC_PASSWORD`, `ADMIN_PASSWORD`) after writing the backup files, so the
sealed value is always the backed-up one, also on a re-run before the first
`secrets.yaml` is pushed. For the admin account, `seal-secrets.sh` seals only the
hash and writes nothing on the Argo CD machine; once the instance holds the
hash, the role passes the hash back unchanged and leaves the backup file alone.
When the backup file disagrees with the instance (restic), the file is rewritten
with the instance's value and the old content kept next to it
(`<file>.<pid>.<date>~`).

Runs before MAIR-599 let `seal-secrets.sh` write the admin plaintext on the
Argo CD machine (`/root/.mairie360/admin-<org>-<env>.txt`, older ones with a date
suffix). The role now fetches those files into `operator_backup_dir` (with a
`.from-<argocd host>` suffix when a local file of that name already exists) and
deletes them from the Argo CD machine.

Rollout on an existing instance: run `playbooks/secrets.yml --limit <host>` once.
It changes no sealed value: `RESTIC_PASSWORD` and the admin hash are read back
from the instance and sealed again as they are; it only creates the backup files
and moves the admin files off the Argo CD machine. Commit the resulting
`secrets.yaml` as usual (its ciphertext changes on every run, its values do not),
then store the files in the team vault.

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
python3 tests/check_image_updater_services.py ../Deploiment  # image_updater_services = services deployed by Deploiment
ansible-lint                                    # pre-existing violations: .ansible-lint, .ansible-lint-ignore
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

## Ajouter un client

Trois fichiers, aucun playbook ni rôle à toucher :

1. `inventory/hosts.yml` (local, gitignored) — les groupes `client_x_argocd` et `client_x_instances` ;
   ajouter aussi le modèle dans `inventory/hosts.example.yml` avec des adresses de documentation
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
