#!/usr/bin/env bash
# MAIR-244: scripts/restore-sealing-key-file.sh puts the sealing keys phase 4
# backed up (`kubectl get secret -l ... -o yaml`, a List with uid,
# resourceVersion, ...) back on a rebuilt machine. A fake `ssh` records what
# reaches the machine.
#
#   bash tests/mair_244_restore_sealing_key_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/restore-sealing-key-file.sh"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
FAILED=0
ok()   { echo "  OK    $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }

mkdir -p "$WORK/bin"
# Fake ssh: logs the remote command, keeps stdin of `apply`, and answers the
# `get deployment` probe with $CONTROLLER (present|absent).
cat > "$WORK/bin/ssh" <<'SH'
#!/usr/bin/env bash
echo "$1 :: $2" >> "$WORK/calls"
case "$2" in
  *" apply -f -") cat > "$WORK/applied.json" ;;
  *"get deployment sealed-secrets-controller") [ "$CONTROLLER" = present ] ;;
esac
SH
chmod +x "$WORK/bin/ssh"
export PATH="$WORK/bin:$PATH" WORK

cat > "$WORK/keys.yaml" <<'YAML'
apiVersion: v1
items:
- apiVersion: v1
  data:
    tls.crt: Y2VydC1h
    tls.key: a2V5LWE=
  kind: Secret
  metadata:
    creationTimestamp: "2026-10-01T10:00:00Z"
    generateName: sealed-secrets-key
    labels:
      sealedsecrets.bitnami.com/sealed-secrets-key: active
    name: sealed-secrets-keyabcde
    namespace: kube-system
    resourceVersion: "1234"
    uid: 0b6c7c1e-0000-4000-8000-000000000001
  type: kubernetes.io/tls
- apiVersion: v1
  data:
    tls.crt: Y2VydC1i
    tls.key: a2V5LWI=
  kind: Secret
  metadata:
    labels:
      sealedsecrets.bitnami.com/sealed-secrets-key: active
    managedFields: [{manager: controller}]
    name: sealed-secrets-keyfghij
    namespace: kube-system
    resourceVersion: "5678"
    uid: 0b6c7c1e-0000-4000-8000-000000000002
  type: kubernetes.io/tls
kind: List
metadata:
  resourceVersion: ""
YAML

# 1. Restore before phase 3: both keys applied, cleaned, no restart.
rm -f "$WORK/calls" "$WORK/applied.json"
if CONTROLLER=absent "$SCRIPT" "$WORK/keys.yaml" root@machine > "$WORK/out" 2>&1; then ok "exit 0 before phase 3"; else fail "exit 0 before phase 3: $(cat "$WORK/out")"; fi
python3 - "$WORK/applied.json" <<'PY' && ok "two keys applied without uid / resourceVersion / managedFields, labels kept" || fail "applied manifest"
import json, sys
d = json.load(open(sys.argv[1]))
items = d["items"]
assert [i["metadata"]["name"] for i in items] == ["sealed-secrets-keyabcde", "sealed-secrets-keyfghij"], items
for i in items:
    m = i["metadata"]
    assert set(m) == {"name", "namespace", "labels"}, m
    assert m["namespace"] == "kube-system"
    assert m["labels"]["sealedsecrets.bitnami.com/sealed-secrets-key"] == "active"
    assert i["data"]["tls.key"] and i["type"] == "kubernetes.io/tls"
PY
grep -q "^root@machine :: sudo -n k3s kubectl apply -f -$" "$WORK/calls" && ok "k3s kubectl on the target" || fail "remote command: $(cat "$WORK/calls")"
grep -q "rollout restart" "$WORK/calls" && fail "restarted a controller that does not exist" || ok "no restart before phase 3"

# 2. Controller already running: restarted so that it loads the keys.
rm -f "$WORK/calls"
CONTROLLER=present "$SCRIPT" "$WORK/keys.yaml" root@machine > "$WORK/out" 2>&1
grep -q "rollout restart deployment sealed-secrets-controller" "$WORK/calls" && ok "controller restarted" || fail "controller not restarted"

# 3. Refusals: nothing reaches the machine.
rm -f "$WORK/calls"
printf 'apiVersion: v1\nkind: List\nitems: []\n' > "$WORK/empty.yaml"
if CONTROLLER=absent "$SCRIPT" "$WORK/empty.yaml" root@machine > /dev/null 2>&1; then fail "empty key file accepted"; else ok "empty key file refused"; fi
if CONTROLLER=absent "$SCRIPT" "$WORK/missing.yaml" root@machine > /dev/null 2>&1; then fail "missing key file accepted"; else ok "missing key file refused"; fi
if CONTROLLER=absent "$SCRIPT" "$WORK/keys.yaml" > /dev/null 2>&1; then fail "missing target accepted"; else ok "usage error without a target"; fi
[ ! -e "$WORK/calls" ] && ok "no SSH call on a refusal" || fail "SSH called on a refusal: $(cat "$WORK/calls")"

[ "$FAILED" -eq 0 ] && echo "OK" || { echo "FAILED"; exit 1; }
