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
| 1 | `k8s_node` | toutes | Durcissement, pare-feu, k3s (Traefik désactivé, ServiceLB actif) |
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
