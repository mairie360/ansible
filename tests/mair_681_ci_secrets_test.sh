#!/usr/bin/env bash
# The release secrets live in the Prod environment (main only) and reach the prod
# promotion alone; pull request runs only get the low-quota key of the AI reviews.
set -u
cd "$(dirname "$0")/.." || exit 1
status=0
fail() { echo "MAIR-681: $1"; status=1; }

for f in .github/workflows/*.yml .github/workflows/*.yaml; do
  [ -f "$f" ] || continue
  grep -qE 'secrets:[[:space:]]*inherit' "$f" && fail "$f uses secrets: inherit"
  case "$(basename "$f")" in promote-prod.yml) continue ;; esac
  grep -qE '^[[:space:]]+(RELEASE_APP_ID|RELEASE_APP_PK|ANTHROPIC_API_KEY):' "$f" && fail "$f passes a release secret"
done
grep -qF '      ANTHROPIC_REVIEW_API_KEY: ${{ secrets.ANTHROPIC_REVIEW_API_KEY }}' .github/workflows/gdpr-pr.yml \
  || fail "gdpr-pr.yml does not pass ANTHROPIC_REVIEW_API_KEY"
[ -e RELEASE_JOURNAL.md ] && fail "RELEASE_JOURNAL.md is still committed"

[ "$status" -eq 0 ] && echo "MAIR-681: workflows list their secrets, release secrets only in promote-prod"
exit "$status"
