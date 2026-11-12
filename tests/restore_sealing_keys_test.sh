#!/usr/bin/env bash
# MAIR-617: scripts/restore-sealing-keys.sh applies every stored sealing key
# through the Argo CD machine and stops at the first failure (CodeRabbit on
# ansible#61: the README loop finished "successfully" with keys missing).
# scw, ssh and jq's input are stubbed; nothing reaches Scaleway or a cluster.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
FAILED=0
ok()   { echo "  OK    $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }

mkdir -p "$WORK/bin"
cat > "$WORK/bin/scw" <<'STUB'
#!/usr/bin/env bash
echo "scw $*" >> "$CALLS"
case "$*" in
  "secret secret list"*) [ "${FAKE_NO_SECRET:-}" = 1 ] && { echo '[]'; exit 0; }; echo '[{"id":"sid"}]' ;;
  "secret version list"*) printf '%s\n' "${FAKE_VERSIONS:-[]}" ;;
  "secret version access"*)
    args="$*"; rev="${args##*revision=}"; rev="${rev%% *}"
    [ "$rev" = "${FAKE_BROKEN_REV:-none}" ] && { echo "denied" >&2; exit 1; }
    echo "kind: Secret # key $rev" ;;
esac
STUB
cat > "$WORK/bin/ssh" <<'STUB'
#!/usr/bin/env bash
echo "ssh $1 ${*:2} <<< $(cat)" >> "$CALLS"
STUB
chmod +x "$WORK/bin/scw" "$WORK/bin/ssh"

run() {
  : > "$WORK/calls"
  env CALLS="$WORK/calls" PATH="$WORK/bin:$PATH" "$@" "$REPO/scripts/restore-sealing-keys.sh" mairie360 dev root@argocd \
    > "$WORK/out" 2>&1
}

echo "== two enabled keys, one disabled"
if run FAKE_VERSIONS='[{"revision":1,"status":"enabled"},{"revision":2,"status":"disabled"},{"revision":3,"status":"enabled"}]'; then
  ok "succeeds"; else fail "failed: $(cat "$WORK/out")"; fi
applied=$(grep -c '^ssh root@argocd kubectl --kubeconfig /root/.kube/instance-dev.yaml apply -f -' "$WORK/calls")
[ "$applied" = 2 ] && ok "keys 1 and 3 applied on the Argo CD machine with the instance kubeconfig" || fail "$applied applies: $(cat "$WORK/calls")"
grep -q 'apply -f - <<< kind: Secret # key 1$' "$WORK/calls" && grep -q 'apply -f - <<< kind: Secret # key 3$' "$WORK/calls" \
  && ok "the content of versions 1 and 3 reached kubectl" || fail "content: $(cat "$WORK/calls")"
grep -q 'key 2' "$WORK/calls" && fail "a disabled version was applied" || ok "the disabled version is skipped"
grep -q 'path=/mairie360/dev name=sealing-keys' "$WORK/calls" && ok "secret looked up under /mairie360/dev" || fail "wrong lookup"

echo "== a version cannot be read: stops"
if run FAKE_VERSIONS='[{"revision":1,"status":"enabled"},{"revision":3,"status":"enabled"}]' FAKE_BROKEN_REV=1; then
  fail "succeeded with a key missing"; else ok "fails"; fi
grep -q 'key 3' "$WORK/calls" && fail "went on after the failure" || ok "stops at the first failure"

echo "== no key stored: fails"
if run FAKE_VERSIONS='[]'; then fail "succeeded with nothing to restore"; else ok "fails"; fi
grep -q "nothing to restore" "$WORK/out" && ok "says so" || fail "message: $(cat "$WORK/out")"

echo "== no sealing-keys secret: fails"
if run FAKE_NO_SECRET=1; then fail "succeeded without the secret"; else ok "fails"; fi
grep -q "terraform apply" "$WORK/out" && ok "points to terraform apply" || fail "message: $(cat "$WORK/out")"

echo "== wrong usage"
run_usage() { "$REPO/scripts/restore-sealing-keys.sh" mairie360 > /dev/null 2>&1; }
run_usage; [ $? = 2 ] && ok "exit 2 without three arguments" || fail "usage not checked"

[ "$FAILED" = 0 ] && echo "PASS" || { echo "FAILED"; exit 1; }
