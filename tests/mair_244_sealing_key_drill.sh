#!/usr/bin/env bash
# MAIR-244 drill of the disaster-recovery runbook (Deploiment
# docs/en/disaster-recovery.md, scenario B steps 4 to 6), on throwaway kind
# clusters, never on a real machine:
#   1. cluster 1: the sealed-secrets controller seals a Secret, the key is saved
#      the way phase 4 does (`kubectl get secret -l ... -o yaml`);
#   2. cluster 1 is deleted, cluster 2 created; scripts/restore-sealing-key-file.sh
#      puts the key back before the controller is installed, then (second pass)
#      after it already runs; the SealedSecret must decrypt both times;
#   3. control: a new cluster without the key must not decrypt it.
# Not run by CI (needs docker, kind, helm, kubeseal and the network). Takes about
# five minutes; deletes its cluster, kubeconfig and key copy on exit.
#
#   bash tests/mair_244_sealing_key_drill.sh
set -euo pipefail
ANS="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"
C=mair244-drill
KC="$W/kubeconfig"
trap 'kind delete cluster --name $C >/dev/null 2>&1 || true; rm -rf "$W"' EXIT
install_ctrl() {
  helm --kubeconfig "$KC" upgrade --install sealed-secrets sealed-secrets --repo https://bitnami.github.io/sealed-secrets \
    --version 2.22.0 -n kube-system --set fullnameOverride=sealed-secrets-controller --wait --timeout 5m >/dev/null
}
newcluster() { kind delete cluster --name $C >/dev/null 2>&1 || true; kind create cluster --name $C --kubeconfig "$KC" >/dev/null; }
# Fake ssh -> docker exec into the kind node (real API server, real kubectl).
cat > "$W/ssh" <<'SH'
#!/usr/bin/env bash
exec docker exec -i mair244-drill-control-plane sh -c "$2"
SH
chmod +x "$W/ssh"
export KUBECTL="kubectl --kubeconfig /etc/kubernetes/admin.conf"

echo "== cluster 1: seal a secret, back the key up the way phase 4 does"
newcluster; install_ctrl
kubectl --kubeconfig "$KC" create secret generic drill --from-literal=k=hello-mair244 --dry-run=client -o yaml \
  | kubeseal --kubeconfig "$KC" --controller-namespace kube-system --controller-name sealed-secrets-controller -n default -o yaml > "$W/sealed.yaml"
kubectl --kubeconfig "$KC" -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > "$W/keys.yaml"
grep -c "tls.key" "$W/keys.yaml"

for mode in before-controller after-controller; do
  echo "== cluster 1 lost; cluster 2, key restored $mode"
  newcluster
  [ $mode = after-controller ] && install_ctrl
  PATH="$W:$PATH" "$ANS/scripts/restore-sealing-key-file.sh" "$W/keys.yaml" root@machine
  [ $mode = before-controller ] && install_ctrl
  kubectl --kubeconfig "$KC" -n kube-system rollout status deploy/sealed-secrets-controller --timeout=3m >/dev/null
  kubectl --kubeconfig "$KC" apply -f "$W/sealed.yaml" >/dev/null
  for i in $(seq 30); do kubectl --kubeconfig "$KC" get secret drill >/dev/null 2>&1 && break; sleep 2; done
  v=$(kubectl --kubeconfig "$KC" get secret drill -o jsonpath='{.data.k}' | base64 -d)
  [ "$v" = hello-mair244 ] && echo "RESULT $mode: decrypted OK" || { echo "RESULT $mode: FAILED"; exit 1; }
done

echo "== control: new cluster, no key restored"
newcluster; install_ctrl
kubectl --kubeconfig "$KC" apply -f "$W/sealed.yaml" >/dev/null; sleep 15
kubectl --kubeconfig "$KC" get secret drill >/dev/null 2>&1 && echo "CONTROL: unexpectedly decrypted" || echo "CONTROL: not decrypted without the key (expected)"
kubectl --kubeconfig "$KC" get sealedsecret drill -o jsonpath='{.status.conditions[0].message}'; echo
