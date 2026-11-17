#!/usr/bin/env bash
# Restore the sealing keys of an instance from the vault (MAIR-617).
#
#   scripts/restore-sealing-keys.sh <org> <env> <ssh target of the group's Argo CD machine>
#   scripts/restore-sealing-keys.sh mairie360 dev root@<argocd machine>
#
# Run it on a reinstalled instance BEFORE Argo CD syncs its sealed-secrets
# controller: the controller generates a new key when it finds none, and the
# committed secrets.yaml then no longer decrypts.
#
# Runs on the operator's workstation (scw CLI configured, like the role); each
# key is applied by kubectl on the Argo CD machine, the only one reaching the
# instance API server, with /root/.kube/instance-<env>.yaml. The keys only
# cross the SSH pipe, nothing is written to disk. Stops at the first failure:
# no key stored, a version that cannot be read, a kubectl apply refused.
set -euo pipefail

if [ $# -ne 3 ]; then
  echo "usage: $0 <org> <env> <ssh target of the Argo CD machine>" >&2
  exit 2
fi
org=$1 env=$2 argocd=$3

id=$(scw secret secret list path="/$org/$env" name=sealing-keys -o json | jq -er '.[0].id') || {
  echo "no sealing-keys secret under /$org/$env (terraform apply in mairie360/Infra?)" >&2
  exit 1
}
revisions=$(scw secret version list "$id" -o json | jq -er '[.[] | select(.status == "enabled") | .revision] | .[]') || {
  echo "no sealing key stored for /$org/$env: nothing to restore" >&2
  exit 1
}

count=0
for rev in $revisions; do
  scw secret version access "$id" revision="$rev" raw=true \
    | ssh "$argocd" kubectl --kubeconfig "/root/.kube/instance-$env.yaml" apply -f -
  count=$((count + 1))
done
echo "$count sealing key(s) of /$org/$env applied. Now let Argo CD sync the instance."
