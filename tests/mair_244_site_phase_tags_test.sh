#!/usr/bin/env bash
# MAIR-244: the disaster-recovery runbook (Deploiment docs/en/disaster-recovery.md)
# rebuilds a lost machine with phases 1, 1b and 2 only, puts the old sealing key
# back, then runs phase 3. That needs each play of playbooks/site.yml to carry
# its phase tag, and a phase tag to select no task of another phase (phase 3 is
# what makes Argo CD deploy the sealed-secrets controller, which generates a new
# key when none exists).
#
#   bash tests/mair_244_site_phase_tags_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
FAILED=0
ERR=$(mktemp); trap 'rm -f "$ERR"' EXIT

# tasks_per_play <tags>: "<play name>=<number of selected tasks>", one per line.
tasks_per_play() {
  ANSIBLE_INVENTORY_UNPARSED_WARNING=false ANSIBLE_LOCALHOST_WARNING=false \
    ansible-playbook playbooks/site.yml --list-tasks --tags "$1" 2>"$ERR" |
    awk '/^  play #/ { sub(/^  play #[0-9]+ \([^)]*\): /, ""); sub(/\t.*/, ""); play = $0; n[play] = 0; order[++k] = play; next }
         /^      [^ ]/ { n[play]++ }
         END { for (i = 1; i <= k; i++) print order[i] "=" n[order[i]] }'
}

# expect <tags> <phases that must have tasks, space separated>
expect() {
  local out line play count phase want
  out=$(tasks_per_play "$1")
  if [ "$(printf '%s\n' "$out" | grep -c .)" -ne 7 ]; then
    echo "  FAIL  --tags $1: expected 7 plays, got:"; printf '%s\n' "$out"
    echo "  ansible-playbook stderr:"; cat "$ERR"; FAILED=1; return
  fi
  while IFS= read -r line; do
    play=${line%=*} count=${line##*=}
    phase=$(printf '%s' "$play" | awk '{print $2}')
    want=0
    for p in $2; do [ "$p" = "$phase" ] && want=1; done
    if [ "$want" = 1 ] && [ "$count" -eq 0 ]; then
      echo "  FAIL  --tags $1: phase $phase selects no task"; FAILED=1
    elif [ "$want" = 0 ] && [ "$count" -ne 0 ]; then
      echo "  FAIL  --tags $1: phase $phase selects $count task(s)"; FAILED=1
    fi
  done <<<"$out"
  echo "  done  --tags $1"
}

expect phase1,phase1b,phase2 "1 1b 2"
expect phase3 "3"
expect phase4 "4"
expect phase5 "5"
# The tag documented before MAIR-244 for the runner alone still works.
expect github_runner "5"
# Phase 6, the operations runner of MAIR-803 (also tag ops_runner).
expect phase6 "6"
expect ops_runner "6"

[ "$FAILED" -eq 0 ] && echo "OK" || { echo "FAILED"; exit 1; }
