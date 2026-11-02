#!/usr/bin/env bash
# MAIR-677: this repository is public and the CI `secret-scan` job (Gitleaks,
# .github/workflows/checks.yml) must block any new commit that adds a secret,
# while letting Ansible Vault ciphertext through (an encrypted vault is meant
# to be committed). Each case commits one file in a throwaway repo and scans it
# with the same Gitleaks image as mairie360/CICD actions/security/gitleaks.
# Needs docker (preinstalled on GitHub runners).
#
#   bash tests/mair_677_secret_scan_test.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Same image as mairie360/CICD actions/security/gitleaks (bump both together).
IMAGE="zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
FAILED=0
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# Fake values built at runtime so this file itself never matches a rule, and
# derived from fixed seeds so a random substring never hits a Gitleaks stopword.
b64() { # b64 <seed> <length>: deterministic high-entropy [A-Za-z0-9] string
  local out="" i=0
  while [ "${#out}" -lt "$2" ]; do
    out+="$(printf '%s-%s' "$1" "$i" | openssl dgst -sha512 -binary | base64 -w0 | tr -d '+/=')"
    i=$((i + 1))
    [ "$i" -lt 100 ] || { echo "b64: no output (openssl missing?)" >&2; return 1; }
  done
  printf '%s' "${out:0:$2}"
}
hex() { # hex <seed> <lines>: Ansible Vault body (80 hex chars per line)
  local i
  for ((i = 0; i < $2; i++)); do
    printf '%s-%s' "$1" "$i" | openssl dgst -sha512 -hex | sed 's/.*= //' | cut -c1-80
  done
}
fake_pat="ghp_$(b64 pat 36)"
key_begin="-----BEGIN OPENSSH ""PRIVATE KEY-----"
key_end="-----END OPENSSH ""PRIVATE KEY-----"

scan() { # scan <expected: clean|leak> <label> <file> <content>
  local repo="$WORK/$RANDOM$RANDOM"
  mkdir -p "$repo/$(dirname "$3")"
  [ -f "$ROOT/.gitleaks.toml" ] && cp "$ROOT/.gitleaks.toml" "$repo/"
  printf '%s\n' "$4" > "$repo/$3"
  git -C "$repo" init -q
  git -C "$repo" -c user.name=t -c user.email=t@t add -A
  git -C "$repo" -c user.name=t -c user.email=t@t commit -qm test
  docker run --rm --user "$(id -u):$(id -g)" -v "$repo:/repo" -w /repo "$IMAGE" \
    git /repo --redact --no-banner --exit-code 1 >/dev/null 2>&1
  local rc=$? got
  if [ "$rc" -eq 0 ]; then got=clean; elif [ "$rc" -eq 1 ]; then got=leak; else got="error($rc)"; fi
  if [ "$got" = "$1" ]; then echo "  OK    $1: $2"; else echo "  FAIL  expected $1, got $got: $2"; FAILED=1; fi
}

scan leak "GHCR PAT in group_vars" inventory/group_vars/all.yml \
  "deploiment_git_token: \"$fake_pat\""

scan leak "SSH private key in the inventory" inventory/files/id_ed25519 \
  "$key_begin
$(b64 sshkey 70)
$(b64 sshkey2 70)
$(b64 sshkey3 70)
$key_end"

scan clean "Ansible Vault ciphertext" inventory/group_vars/all/vault.yml \
  "\$ANSIBLE_VAULT;1.1;AES256
$(hex vault 12)"

scan leak "PAT in a role default next to vaulted variables" roles/k8s_argocd/defaults/main.yml \
  "argocd_admin_password: \"{{ vault_argocd_admin_password }}\"
ghcr_token: \"$fake_pat\""

exit "$FAILED"
