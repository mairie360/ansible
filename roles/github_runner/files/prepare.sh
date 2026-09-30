#!/usr/bin/env bash
# Installed by Ansible (role github_runner) — do not edit on the machine.
#
# ExecStartPre of gh-runner.service, run as root (the `+` prefix) before
# every job:
#   1. gives the runner a pristine copy of the pinned release and an empty
#      HOME, so nothing a job wrote (runner binaries, git config, caches,
#      checkouts) survives into the next one;
#   2. registers a just-in-time (single-job, ephemeral) runner on the
#      repository with the GitHub App of /etc/gh-runner, and hands its
#      config to the runner through /run/gh-runner/jitconfig.
# The App key never leaves /etc/gh-runner (root, 0700), which the runner
# user cannot read. Never add `set -x` here: it would print the tokens.
set -euo pipefail
umask 077

: "${RUNNER_REPO:?}" "${RUNNER_LABELS:?}" "${RUNNER_NAME_PREFIX:?}" "${RUNNER_DIST:?}" "${RUNNER_USER:?}"
KEY=/etc/gh-runner/app.pem
APP_ID=$(cat /etc/gh-runner/app-id)
STATE=/var/lib/gh-runner
API=https://api.github.com

# --- 1. Pristine runner and HOME ---------------------------------------------
rm -rf "${STATE:?}/runner" "${STATE:?}/home"
install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0700 "$STATE/runner" "$STATE/home"
cp -a "$RUNNER_DIST/." "$STATE/runner/"
chown -R "$RUNNER_USER:$RUNNER_USER" "$STATE/runner"

# --- 2. Just-in-time registration ---------------------------------------------
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
api() {
  curl -fsS --retry 3 --max-time 30 \
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "$@"
}

# App JWT (RS256, 10 minutes max, backdated 60 s for clock drift).
now=$(date +%s)
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$APP_ID" | b64url)
signature=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$KEY" -binary | b64url)
jwt="$header.$payload.$signature"

# Installation token scoped down to this one repository and to the only
# permission registration needs.
installation=$(api -H "Authorization: Bearer $jwt" "$API/repos/$RUNNER_REPO/installation" | jq -r .id)
token=$(api -X POST -H "Authorization: Bearer $jwt" "$API/app/installations/$installation/access_tokens" \
  -d "$(jq -nc --arg repo "${RUNNER_REPO#*/}" '{repositories: [$repo], permissions: {administration: "write"}}')" \
  | jq -r .token)

name="$RUNNER_NAME_PREFIX-$(date -u +%Y%m%d%H%M%S)-$RANDOM"
body=$(jq -nc --arg name "$name" --arg labels "$RUNNER_LABELS" \
  '{name: $name, runner_group_id: 1, labels: ($labels | split(",")), work_folder: "_work"}')
jit=$(api -X POST -H "Authorization: Bearer $token" \
  "$API/repos/$RUNNER_REPO/actions/runners/generate-jitconfig" -d "$body" | jq -r .encoded_jit_config)

# The installation token is not needed any more: revoke it.
api -X DELETE -H "Authorization: Bearer $token" "$API/installation/token" >/dev/null || true

[ -n "$jit" ] && [ "$jit" != null ] || { echo "no JIT config returned for $name" >&2; exit 1; }
install -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0600 /dev/null /run/gh-runner/jitconfig
printf '%s' "$jit" > /run/gh-runner/jitconfig
echo "registered ephemeral runner $name on $RUNNER_REPO (labels: $RUNNER_LABELS)"
