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

## Les trois phases

| Phase | Rôle | Cible | Ce qu'elle fait |
|---|---|---|---|
| 1 | `k8s_node` | all | Hardening, firewall, k3s (Traefik disabled, ServiceLB on), Cilium CNI + Hubble |
| 1b | `wireguard` | toutes | Tunnel du groupe : l'Argo CD écoute, les instances s'y connectent |
| 2 | `k8s_argocd` | `*_argocd` | Argo CD + `platform-app.yaml` + l'ApplicationSet du groupe |
| 3 | `k8s_instance_link` | `*_instances` | `argocd cluster add` sur l'IP WireGuard + étiquetage |

Le rôle `k8s_argocd` n'installe **ni** cert-manager, **ni** ingress-nginx,
**ni** de ClusterIssuer : c'est Argo CD qui les déploie sur les instances,
depuis `Deploiment`. Les installer ici créerait des doublons en conflit
(versions différentes, CRD concurrentes).

## Utilisation

```bash
ansible-galaxy collection install -r requirements.yml

# Tout déployer
ansible-playbook playbooks/site.yml

# Un seul groupe (ajout d'un client, par exemple)
ansible-playbook playbooks/site.yml --limit 'client_example_argocd:client_example_instances'

# Recette
ansible-playbook playbooks/verify.yml
```

Puis, pour chaque instance, générer ses secrets depuis le dépôt `Deploiment` :

```bash
./scripts/seal-secrets.sh <contexte-kube> <org> <env>
git add clusters/<org>/instances/<env>/secrets.yaml && git commit && git push
```

Tant que ce fichier n'existe pas, les pods restent en
`CreateContainerConfigError` — c'est attendu, pas un bug.

## Ajouter un client

Trois fichiers, aucun playbook ni rôle à toucher :

1. `inventory/hosts.yml` — les groupes `client_x_argocd` et `client_x_instances`
2. `group_vars/client_x_argocd.yml` et `group_vars/client_x_instances.yml` —
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
`hubble_cli_version` in `group_vars/all.yml`.

## Conventions qui comptent

**`org_id` garde les tirets** (`client-example`) parce qu'il doit correspondre
au dossier `clusters/<org_id>/` de `Deploiment`. Les noms de groupes Ansible
n'acceptent que des underscores. `org_group` (défini dans `group_vars/all.yml`)
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

**`ansible_host` sert au SSH ; `wg_ip` sert à Argo CD.** Le port 6443 donne un
accès administrateur : il ne doit jamais être joignable depuis Internet.

## Fermeture du SSH public

`k8s_node` laisse volontairement le port 22 ouvert : couper le SSH public avant
d'avoir validé le tunnel verrouillerait la machine. Une fois la connexion par
l'IP WireGuard confirmée :

```bash
ansible-playbook playbooks/site.yml -e wireguard_close_public_ssh=true
```

Le rôle vérifie que le tunnel répond avant de retirer la règle.
