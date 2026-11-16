#!/usr/bin/env bash
# MAIR-800: inventory/hosts.yml is gitignored since MAIR-708 and, on
# 2026-11-06, had to be dug out of the git history. scripts/restore-inventory.py
# must rebuild it from hosts.example.yml and the Scaleway servers' public IPv4,
# fail clearly when a server is missing, never overwrite by accident and never
# write to a path git would commit. A fake `scw` answers with documentation
# addresses (RFC 5737), so no real one is involved.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# A throwaway copy of the repository, so a real inventory/hosts.yml is never
# touched and the gitignore rules are the repository's own.
ROOT="$TMP/repo"
mkdir -p "$ROOT/scripts" "$ROOT/inventory/group_vars"
cp "$REPO/.gitignore" "$ROOT/"
cp "$REPO/scripts/restore-inventory.py" "$ROOT/scripts/"
cp "$REPO/inventory/hosts.example.yml" "$ROOT/inventory/"
cp "$REPO/inventory/group_vars/all.yml" "$ROOT/inventory/group_vars/"
git -C "$ROOT" init -q
git -C "$ROOT" add -A
git -C "$ROOT" -c user.name=t -c user.email=t@example.invalid commit -qm init
SCRIPT="$ROOT/scripts/restore-inventory.py"
OUT="$ROOT/inventory/hosts.yml"
failed=0
ok() { if [ "$1" = 0 ]; then echo "  OK    $2"; else echo "  FAIL  $2"; failed=1; fi; }

server() { # server <name> <ipv4>
  printf '{"name": "%s", "public_ips": [{"family": "inet6", "address": "2001:db8::1"}, {"family": "inet", "address": "%s"}]}' "$1" "$2"
}
cat > "$TMP/scw" <<'SH'
#!/usr/bin/env bash
[ "$*" = "instance server list zone=all -o json" ] || { echo "unexpected: $*" >&2; exit 2; }
cat "$SCW_JSON"
SH
chmod +x "$TMP/scw"
export SCW_BIN="$TMP/scw" SCW_JSON="$TMP/servers.json"

echo "[$(server 'argocd hub' 192.0.2.10), $(server mairie360-dev 192.0.2.11), $(server mairie360-staging 192.0.2.12), $(server mairie360-prod 192.0.2.13), $(server 'mairie360 n8n' 192.0.2.99)]" > "$SCW_JSON"

python3 "$SCRIPT" > "$TMP/out" 2>&1; ok "$?" "writes inventory/hosts.yml"
host_ip() { ansible-inventory -i "$OUT" --host "$2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ansible_host"])'; }
ok "$([ "$(host_ip mairie360_argocd mairie360-argocd)" = 192.0.2.10 ] && echo 0 || echo 1)" "argocd host gets the IPv4 of server 'argocd hub' (scw_server)"
ok "$([ "$(host_ip mairie360_instances mairie360-prod)" = 192.0.2.13 ] && echo 0 || echo 1)" "an instance gets the IPv4 of the server of the same name, not its IPv6"
ok "$(! grep -q 203.0.113 "$OUT" && echo 0 || echo 1)" "no documentation address left"
ok "$(diff <(grep -v ansible_host "$ROOT/inventory/hosts.example.yml") <(grep -v ansible_host "$OUT" | tail -n +4) >/dev/null && echo 0 || echo 1)" "everything else is the example, unchanged"
ok "$(ansible-inventory -i "$OUT" --list >/dev/null 2>&1 && echo 0 || echo 1)" "ansible reads it"
ok "$(! grep -q 192.0.2 "$TMP/out" && echo 0 || echo 1)" "no address printed"
ok "$(git -C "$ROOT" status --porcelain -- inventory | grep -q hosts.yml && echo 1 || echo 0)" "git does not see it"

python3 "$SCRIPT" > "$TMP/out" 2>&1; code=$?
ok "$([ "$code" != 0 ] && grep -q 'already exists' "$TMP/out" && echo 0 || echo 1)" "refuses to overwrite without --force"
python3 "$SCRIPT" --check > "$TMP/out" 2>&1; ok "$?" "--check passes when the addresses match"

echo "[$(server 'argocd hub' 192.0.2.10), $(server mairie360-dev 192.0.2.21), $(server mairie360-staging 192.0.2.12), $(server mairie360-prod 192.0.2.13)]" > "$SCW_JSON"
python3 "$SCRIPT" --check > "$TMP/out" 2>&1; code=$?
ok "$([ "$code" != 0 ] && grep -q 'differs from Scaleway (or is missing) for: mairie360-dev' "$TMP/out" && echo 0 || echo 1)" "--check names a machine whose address changed"
python3 "$SCRIPT" --force > "$TMP/out" 2>&1
ok "$([ "$(host_ip mairie360_instances mairie360-dev)" = 192.0.2.21 ] && echo 0 || echo 1)" "--force replaces it"

echo "[$(server 'argocd hub' 192.0.2.10), $(server mairie360-dev 192.0.2.11), $(server mairie360-prod 192.0.2.13)]" > "$SCW_JSON"
python3 "$SCRIPT" --force > "$TMP/out" 2>&1; code=$?
ok "$([ "$code" != 0 ] && grep -q "mairie360-staging (server 'mairie360-staging')" "$TMP/out" && echo 0 || echo 1)" "fails naming a host with no Scaleway server"

echo "[$(server 'argocd hub' 192.0.2.10), $(server mairie360-dev 192.0.2.11), $(server mairie360-dev 192.0.2.31), $(server mairie360-staging 192.0.2.12), $(server mairie360-prod 192.0.2.13)]" > "$SCW_JSON"
python3 "$SCRIPT" --force > "$TMP/out" 2>&1; code=$?
ok "$([ "$code" != 0 ] && grep -q "several servers are named 'mairie360-dev'" "$TMP/out" && echo 0 || echo 1)" "an ambiguous server name is refused"

python3 "$SCRIPT" --out "$ROOT/inventory/group_vars/all.yml" --force > "$TMP/out" 2>&1; code=$?
ok "$([ "$code" != 0 ] && grep -q 'not ignored by git' "$TMP/out" && echo 0 || echo 1)" "refuses to write to a tracked file"
ok "$(git -C "$ROOT" diff --quiet -- inventory/group_vars/all.yml && echo 0 || echo 1)" "...which is left untouched"

SCW_BIN=false python3 "$SCRIPT" --force > "$TMP/out" 2>&1; code=$?
ok "$([ "$code" != 0 ] && grep -q 'scw init' "$TMP/out" && echo 0 || echo 1)" "a failing scw says to run scw init"

exit "$failed"
