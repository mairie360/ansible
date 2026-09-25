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
| 2 | `k8s_argocd` | `*_argocd` | Argo CD + `platform-app.yaml` + the group's ApplicationSet, `argocd/ghcr-secret`, one `ImageUpdater` per instance |
| 3 | `k8s_instance_link` | `*_instances` | `argocd cluster add` on the WireGuard IP + labels |
| 4 | `k8s_instance_secrets` | `*_instances` | Seals the instance secrets with `seal-secrets.sh`, asks for the missing external ones, copies `secrets.yaml` back to the local `Deploiment` checkout |

Le rôle `k8s_argocd` n'installe **ni** cert-manager, **ni** ingress-nginx,
**ni** de ClusterIssuer : c'est Argo CD qui les déploie sur les instances,
depuis `Deploiment`. Les installer ici créerait des doublons en conflit
(versions différentes, CRD concurrentes).

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
  `AWS_SECRET_ACCESS_KEY`), the value already on the instance, otherwise
  **a prompt** (empty to skip). The backup bucket keys are only asked when
  `backup.enabled` is true in the instance values;
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
| `k3s_version` | `v1.35.8+k3s1` | Kubernetes 1.35 is supported upstream until 2027-02-28 and is the only minor inside every matrix of the stack: Cilium 1.20 (1.33-1.36), Argo CD 3.4 (1.32-1.35), cert-manager 1.21 (1.33-1.36) and ingress-nginx controller 1.15 (1.31-1.35), both deployed by `Deploiment` |
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
