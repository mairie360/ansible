# ansible

Provisioning of the Mairie360 machines. This repo **prepares the servers and
bootstraps Kubernetes**; the desired state of the applications lives in
[`mairie360/Deploiment`](https://github.com/mairie360/Deploiment).

## Model

**One Argo CD and one Mairie360 instance per machine.** A *group* = one Argo
CD + its instances.

```
Group mairie360 (4 machines)       Group client-x (2 machines)
  mairie360-argocd                   client-x-argocd
  mairie360-dev                      client-x-prod
  mairie360-staging
  mairie360-prod
```

Each Argo CD only drives the instances of its group, and its WireGuard tunnel
only spans that group. A compromised client has no network route to another
one: the isolation comes from the topology. The admin workstations are peers
of that tunnel too, and Ansible reaches every machine through it (see "SSH
access").

## The four phases

| Phase | Role | Target | What it does |
|---|---|---|---|
| 1 | `k8s_node` | all | Hardening (sshd public key only, no root), firewall (SSH only from the admin peers over `wg0`, 6443 only from the group's Argo CD over `wg0`), k3s (Secrets encrypted at rest, Traefik disabled, ServiceLB on), Cilium CNI + Hubble |
| 1b | `wireguard` | all | Group tunnel: the Argo CD machine listens, the instances and the admin workstations connect to it; it routes the admins to the instances (SSH only) |
| 2 | `k8s_argocd` | `*_argocd` | Argo CD + `platform-app.yaml` + the group's ApplicationSet, `argocd/ghcr-secret`, `argocd/deploiment-git-creds`, one `ImageUpdater` per followed environment (dev, staging; never prod) |
| 3 | `k8s_instance_link` | `*_instances` | Short-lived instance kubeconfig on the Argo CD machine, `argocd cluster add` on the WireGuard IP + labels (`mairie360.fr/role`, `org`, `env`, `ingress`) |
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
`inventory/group_vars/all/main.yml`). Phase 3 writes it as the Argo CD cluster
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

Every command needs the vault password (the public IPs live in
`inventory/group_vars/all/vault.yml`): `--ask-vault-pass`, or
`ANSIBLE_VAULT_PASSWORD_FILE` pointing at a file only you can read. Your
workstation's WireGuard tunnel must be up (see "SSH access").

```bash
ansible-galaxy collection install -r requirements.yml

# Deploy everything (asks for the secrets it cannot find)
ansible-playbook playbooks/site.yml --ask-vault-pass

# One group (adding a client, for instance). Always whole groups: the
# WireGuard phase needs every machine of the group and fails otherwise.
ansible-playbook playbooks/site.yml --ask-vault-pass --limit 'client_example_argocd:client_example_instances'

# Acceptance test (runs Deploiment/scripts/verify.sh from the Argo CD machine,
# and checks SSH exposure, sshd and Secrets encryption on every machine)
ansible-playbook playbooks/verify.yml --ask-vault-pass
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
python3 tests/check_no_log.py     # fails on a secret-handling task without no_log
python3 tests/check_inventory.py  # fails on a clear vault or a public IP in the repo
```

## Adding a client

No playbook or role to touch:

1. `inventory/hosts.yml`: the parent group `client_x` with its children
   `client_x_argocd` and `client_x_instances` (own WireGuard subnet, e.g.
   `10.10.1.0/24`), following the commented template;
2. `inventory/group_vars/client_x.yml`, from `client-example.yml.example`:
   `org_id: client-x` and the admin peers of that group;
3. the public IPs in the vault: `ansible-vault edit inventory/group_vars/all/vault.yml`;
4. in `Deploiment`: `clusters/client-x/instances/prod/values.yaml`.

The first run is a bootstrap run (no tunnel yet), then a normal one closes
public SSH:

```bash
ansible-playbook playbooks/site.yml --ask-vault-pass --limit 'client_x_argocd:client_x_instances' -e ssh_via_public_ip=true
ansible-playbook playbooks/site.yml --ask-vault-pass --limit 'client_x_argocd:client_x_instances'
```

A client `prod` gets no `ImageUpdater`: its versions only move through
`Deploiment` (see "Image versions per environment").

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
`hubble_cli_version` in `inventory/group_vars/all/main.yml` (see "Upgrading k3s and
Argo CD" for `k3s_version` / `argocd_version`).

## Upgrading k3s and Argo CD

Versions are pinned in `inventory/group_vars/all/main.yml` and bumped through a PR:

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

## Conventions that matter

**`org_id` keeps its dashes** (`client-example`) because it must match the
`clusters/<org_id>/` folder of `Deploiment`. Ansible group names only accept
underscores. `org_group` (defined in `inventory/group_vars/all/main.yml`)
bridges both: use it in `groups[...]`, never `org_id`.

**`env_name` is the cluster name in Argo CD.** The ApplicationSet targets
`destination.name: '{{ .path.basename }}'`, i.e. the name of the
`clusters/<org>/instances/<env>/` folder. Registering under any other name
(such as `inventory_hostname`) leaves the Application stuck on "cluster does
not exist", with no other symptom. The `k8s_instance_link` role forces the
right name.

**The `mairie360.fr/role=instance` label** tells an instance apart from the
Argo CD machine. The base AppSets select on it: without it, nothing of the
base layer is deployed on the instance.

**`public_ip` is for the WireGuard endpoint, `wg_ip` for everything else.**
Ansible SSH (`ansible_host`) and Argo CD both go through the tunnel. Port 6443
gives administrator access: it is only open on `wg0`, and only to the group's
Argo CD tunnel IP, never to the Internet.

## SSH access

Since MAIR-415, no machine accepts SSH from the Internet. Ansible connects to
`wg_ip` from a workstation that is a peer of the group's tunnel:

- the workstations are listed in `wireguard_admin_peers`
  (`inventory/group_vars/<org>.yml`): name, WireGuard public key, tunnel IP;
- the Argo CD machine routes them to the instances, SSH only. UFW accepts SSH
  on `wg0` from those IPs only: the machines of a group cannot SSH into each
  other (a compromised dev reaches neither the Argo CD machine nor prod);
- sshd only accepts public keys, never `root` (`/etc/ssh/sshd_config.d/00-mairie360.conf`);
- host keys are checked (`ansible.cfg`): accepted on first contact, a changed
  key fails the run. Over the tunnel the machine is already authenticated by
  its WireGuard key;
- the public IPs are in the ansible-vault file
  `inventory/group_vars/all/vault.yml`, not in the inventory. They stay in the
  git history before MAIR-415, and the instance ones are public anyway through
  their DNS name: the gain is not publishing the Argo CD machine and no longer
  pairing every address with its role.

**Bootstrap run.** `-e ssh_via_public_ip=true` connects through the public
IPs and keeps port 22 open publicly. It is the only way in for a new machine
(no tunnel yet), or for a group whose admin peers changed in a way that cuts
you off. The next normal run closes port 22 again. `playbooks/preflight.yml`
refuses a normal run on a group without any admin peer.

### Joining the tunnel from a workstation

```bash
# 1. A key pair; the private key never leaves the workstation.
umask 077; mkdir -p ~/.mairie360
wg genkey | tee ~/.mairie360/wg-mairie360.key | wg pubkey
# 2. Add yourself to inventory/group_vars/mairie360.yml (PR), with a free IP
#    in 10.10.0.100-199:
#      wireguard_admin_peers:
#        - { name: <you>, public_key: "<output above>", wg_ip: "10.10.0.1xx" }
# 3. Someone already in the tunnel applies it (phase 1 and 1b of site.yml).
```

Phase 1b prints the `[Peer]` block for the workstation. The configuration,
e.g. `/etc/wireguard/mairie360.conf`, then `sudo wg-quick up mairie360`:

```ini
[Interface]
PrivateKey = <content of ~/.mairie360/wg-mairie360.key>
Address = 10.10.0.1xx/32

[Peer]
PublicKey = <printed by phase 1b: /etc/wireguard/public.key of the Argo CD machine>
Endpoint = <public IP of mairie360-argocd, from the vault>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```

`ssh ubuntu@10.10.0.11` must then reach dev. The Argo CD UI is reached the
same way: `ssh -L 8080:localhost:8080 ubuntu@10.10.0.1`, then
`sudo kubectl -n argocd port-forward svc/argocd-server 8080:443`.

### Migrating machines provisioned before MAIR-415

In this order, whole groups, from a workstation that still has public SSH:

```bash
# 1. Encrypt the vault (team vault password) and add your admin peer (above).
ansible-vault encrypt inventory/group_vars/all/vault.yml
# 2. Bootstrap run: admin peers and routing, sshd, Secrets encryption.
#    Port 22 stays public.
ansible-playbook playbooks/site.yml --ask-vault-pass -e ssh_via_public_ip=true
# 3. Bring your tunnel up and check every machine answers through it.
sudo wg-quick up mairie360
for ip in 10.10.0.1 10.10.0.11 10.10.0.12 10.10.0.13; do ssh -o BatchMode=yes ubuntu@$ip true && echo "$ip ok"; done
# 4. Normal run: through the tunnel, closes port 22 publicly.
ansible-playbook playbooks/site.yml --ask-vault-pass
ansible-playbook playbooks/verify.yml --ask-vault-pass
```

## Secrets encryption at rest

k3s runs with `secrets-encryption: true` (`k8s_node`): Secrets are stored
encrypted in the k3s datastore (key in `/var/lib/rancher/k3s/server/cred/`,
backed up with the machine, never in git). A new machine is encrypted from its
first start. A machine started without it goes through the k3s migration on
the next `site.yml` run: `k3s secrets-encrypt enable`, a k3s restart with the
flag, `rotate-keys` (re-encrypts every existing Secret, about 5 per second),
another restart. Two short API server interruptions per machine; the pods keep
running. Do `dev` first:

```bash
ansible-playbook playbooks/site.yml --ask-vault-pass --limit 'mairie360_argocd:mairie360_instances'
sudo k3s secrets-encrypt status   # on a machine: "Encryption Status: Enabled"
```

## Image versions per environment

| Environment | Who moves the image tags | Where they live |
|---|---|---|
| `dev` | argocd-image-updater, newest `dev-<sha>`, as a **pull request** on `Deploiment` `main`, auto-merged once CI passes | `clusters/<org>/instances/dev/values.yaml`, deployed by dev's auto-sync |
| `staging` | argocd-image-updater, newest `staging-<sha>`, as a **commit on the `staging` branch** | `clusters/<org>/instances/staging/images.yaml` (staging branch only), deployed by staging's auto-sync |
| `prod`, client instances | nobody: no `ImageUpdater` | `clusters/<org>/instances/<env>/values.yaml`, promoted `main` -> `staging` -> `prod` like any change |

The policies are `image_updater_policies` (`roles/k8s_argocd/defaults/main.yml`):
an environment that is not listed is never touched by argocd-image-updater.
Since MAIR-444 every tracked image includes `database` (the Postgres server:
a new tag restarts it) and `liquibase-migrations` (applied by the next sync),
and dev and staging both write back to git: what runs there is in git. The
staging commits never touch `main`; Deploiment's `Promote` merges `main` into
`staging` while `staging` is ahead by those commits only (Deploiment ADR 0002,
amendment). Both environments sync by themselves
(`deploiment_auto_sync_envs`).
Prod therefore runs the exact combination of tags committed in `Deploiment`,
reviewed and promoted through staging, and a `Promote` rollback also rolls the
images back.

The dev pull requests and the staging commits need
`argocd/deploiment-git-creds`: a fine-grained token on `mairie360/Deploiment`
with contents and pull requests read/write (the `staging` ruleset only forbids
force-push and deletion, so the commits go through),
from `DEPLOIMENT_GIT_USER` / `DEPLOIMENT_GIT_TOKEN`, else kept from the
machine, else prompted (phase 2).

**Migrating an Argo CD provisioned before MAIR-415.** The tags a former
`argocd` write-back stored in the prod and staging Applications still override
their `values.yaml`. Phase 2 deletes the prod `ImageUpdater` and prints, per
Application, the tags it still carries. Pin those in `Deploiment` (PR, then
promote), then remove them:

```bash
ansible-playbook playbooks/site.yml --ask-vault-pass --limit 'mairie360_argocd:mairie360_instances' -e image_updater_clear_parameters=true
```

prod is synced by hand: nothing changes on the cluster until the next sync,
which then applies `values.yaml`. Check the Argo CD diff shows no image change
before syncing. **MAIR-444:** dev and staging switch from `argocd` to `git`
write-back: run the same command once to drop their parameters. dev and
staging briefly fall back on the tags of their `values.yaml`, until the next
image-updater pass commits the newest ones (a PR for dev, a commit for
staging).

## Instance kubeconfigs on the Argo CD machine

`/root/.kube/instance-<env>.yaml` (used by `argocd cluster add`, phase 4 and
`verify.yml`) holds a token of the `kube-system/mairie360-ops` ServiceAccount
that expires after `instance_ops_token_duration` (2 h), rewritten by every
playbook that needs it. It used to be the k3s admin client certificate: valid
for a year and impossible to revoke. To also invalidate the certificates
copied before MAIR-415, rotate the k3s client certificates on each instance
(`k3s certificate rotate` with k3s stopped, `dev` first).

Argo CD itself uses the `kube-system/argocd-manager` ServiceAccount that
`argocd cluster add` creates on each instance. The Argo CD machine still holds
those tokens for dev, staging and prod: a separate Argo CD group for prod is
the next step to break that concentration.
