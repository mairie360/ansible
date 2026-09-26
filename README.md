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
  `AWS_SECRET_ACCESS_KEY`), the value already on the instance, otherwise
  **a prompt** (empty to skip). The backup bucket keys are only asked when
  `backup.enabled` is true in the instance values;
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
`hubble_cli_version` in `inventory/group_vars/all.yml`.

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
