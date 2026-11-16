#!/usr/bin/env bash
# Put the sealing key(s) of a lost instance back on its rebuilt machine (MAIR-244).
#
#   scripts/restore-sealing-key-file.sh <key file> <ssh target of the instance machine>
#   scripts/restore-sealing-key-file.sh ~/.mairie360/sealing-keys/mairie360-dev.yaml root@<instance machine>
#
# <key file> is the copy phase 4 wrote (`kubectl get secret -l
# sealedsecrets.bitnami.com/sealed-secrets-key -o yaml`, kept in the team vault).
# Run it after phases 1, 1b and 2 and BEFORE phase 3: phase 3 makes Argo CD deploy
# the sealed-secrets controller, which generates a new key when it finds none, and
# the committed secrets.yaml would not decrypt. If the controller already runs,
# the script restarts it so that it loads the restored keys (it keeps its own
# key too, nothing is lost). See Deploiment docs/en/disaster-recovery.md.
#
# The keys are cleaned of the fields the API server refuses on a create (uid,
# resourceVersion, ...) and sent through the SSH pipe to the k3s kubectl of the
# machine: nothing is written to disk on either side. Needs python3 with PyYAML
# (installed with ansible) on the workstation. KUBECTL overrides the remote
# command (default `sudo -n k3s kubectl`).
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <key file> <ssh target of the instance machine>" >&2
  exit 2
fi
file=$1 target=$2
kubectl_cmd=${KUBECTL:-sudo -n k3s kubectl}
[ -r "$file" ] || { echo "cannot read $file" >&2; exit 1; }

manifest=$(python3 - "$file" <<'PY'
import json, sys, yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
items = doc.get("items", []) if isinstance(doc, dict) and doc.get("kind") == "List" else [doc]
keys = []
for item in items:
    if not isinstance(item, dict) or item.get("kind") != "Secret":
        continue
    meta = item.get("metadata", {})
    labels = meta.get("labels") or {}
    if "sealedsecrets.bitnami.com/sealed-secrets-key" not in labels:
        continue
    if not (item.get("data") or {}).get("tls.key"):
        continue
    keys.append({
        "apiVersion": "v1",
        "kind": "Secret",
        "type": item.get("type", "kubernetes.io/tls"),
        "metadata": {"name": meta["name"], "namespace": "kube-system", "labels": labels},
        "data": item["data"],
    })
if not keys:
    sys.exit("no sealed-secrets key in " + sys.argv[1])
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": keys}))
PY
)
count=$(printf '%s' "$manifest" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["items"]))')

# shellcheck disable=SC2029 # kubectl_cmd is meant to expand on the client side
printf '%s' "$manifest" | ssh "$target" "$kubectl_cmd apply -f -"
# A controller that already runs only reads its keys at start.
# shellcheck disable=SC2029
if ssh "$target" "$kubectl_cmd -n kube-system get deployment sealed-secrets-controller" >/dev/null 2>&1; then
  ssh "$target" "$kubectl_cmd -n kube-system rollout restart deployment sealed-secrets-controller"
  echo "$count sealing key(s) restored, sealed-secrets-controller restarted."
else
  echo "$count sealing key(s) restored. Now run phase 3 (--tags phase3)."
fi
