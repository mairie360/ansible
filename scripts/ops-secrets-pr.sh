#!/usr/bin/env bash
# Pull request on Deploiment with a secrets.yaml sealed by the Operations workflow (MAIR-803).
#
#   ORG=<org> ENV_NAME=<env> GH_TOKEN=<token> scripts/ops-secrets-pr.sh <Deploiment checkout> <secrets.yaml>
#
# Copies the file to clusters/<org>/instances/<env>/secrets.yaml, commits it on a new branch
# and opens a pull request against main. Nothing to do when the file is unchanged.
# Optional: ACTOR and RUN_URL (shown in the description), GITHUB_RUN_ID (branch name),
# DEPLOIMENT_REPO (owner/name, default mairie360/Deploiment), PUSH_URL (tests).
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <Deploiment checkout> <secrets.yaml>" >&2
  exit 2
fi
checkout=$1
sealed=$2
: "${ORG:?}" "${ENV_NAME:?}" "${GH_TOKEN:?}"
repo=${DEPLOIMENT_REPO:-mairie360/Deploiment}

# Both end up in a path and a branch name.
for value in "$ORG" "$ENV_NAME"; do
  [[ "$value" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "invalid name '$value'" >&2; exit 2; }
done
grep -Eq '^[[:space:]]*kind: SealedSecret[[:space:]]*$' "$sealed" || { echo "$sealed holds no SealedSecret" >&2; exit 1; }

target="clusters/$ORG/instances/$ENV_NAME/secrets.yaml"
[ -d "$checkout/$(dirname "$target")" ] || { echo "$target: no such instance in Deploiment" >&2; exit 1; }
cp "$sealed" "$checkout/$target"

cd "$checkout"
if git diff --quiet -- "$target" && ! git ls-files --others --exclude-standard -- "$target" | grep -q .; then
  echo "$target is unchanged: no pull request."
  exit 0
fi

branch="ops/seal-secrets-$ORG-$ENV_NAME-${GITHUB_RUN_ID:-$(date -u +%Y%m%d%H%M%S)}"
title="chore($ENV_NAME): seal secrets"
git switch --quiet -c "$branch"
git add -- "$target"
git -c user.name="mairie360-ops" -c user.email="mairie360-ops@users.noreply.github.com" \
  commit --quiet -m "$title" -m "Sealed by the Operations workflow of mairie360/ansible (MAIR-803) for $ORG-$ENV_NAME."
git push --quiet "${PUSH_URL:-https://x-access-token:$GH_TOKEN@github.com/$repo.git}" "HEAD:refs/heads/$branch"

body="Sealed by the [Operations workflow](${RUN_URL:-}) of mairie360/ansible (MAIR-803), started by ${ACTOR:-unknown} and approved in its environment.

Only \`$target\` changes: SealedSecrets encrypted with the instance's key. Merging it lets Argo CD apply them to $ORG-$ENV_NAME."
gh pr create --repo "$repo" --base main --head "$branch" --title "$title" --body "$body"
