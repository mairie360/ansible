#!/usr/bin/env bash
# MAIR-242: the `ufw_public_profiles` collector of playbooks/verify.yml lists the application
# profiles open to everyone (they have no port number, the numeric port check cannot see them),
# with or without a rule comment, and nothing else. The command is read from the playbook itself
# and run against a fake `ufw status`, so the test cannot drift from what the machines run.
#
#   bash tests/ufw_public_profiles_test.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LINE="$(grep 'ufw_public_profiles=\$(ufw status' "$ROOT/playbooks/verify.yml")"
[ -n "$LINE" ] || { echo "FAIL: collector not found in playbooks/verify.yml"; exit 1; }

STATUS=""
ufw() { [ "$1" = status ] && printf '%s' "$STATUS"; }
collect() { STATUS="$1"; eval "$LINE"; }

FAILED=0
expect() { # expect <label> <expected output> <ufw status text>
  local got
  got="$(collect "$3")"
  if [ "$got" = "ufw_public_profiles=$2" ]; then echo "ok   $1"; else echo "FAIL $1: got '$got', want 'ufw_public_profiles=$2'"; FAILED=1; fi
}

HEADER=$'Status: active\n\nTo                         Action      From\n--                         ------      ----\n'
expect "ports only: no profile" "" "${HEADER}22/tcp                     ALLOW       Anywhere"$'\n'"80/tcp                     ALLOW       Anywhere"$'\n'
expect "a profile" "Samba" "${HEADER}22/tcp                     ALLOW       Anywhere"$'\n'"Samba                      ALLOW       Anywhere"$'\n'
expect "a profile with a space" "Nginx Full" "${HEADER}Nginx Full                 ALLOW       Anywhere"$'\n'
expect "a profile with a rule comment" "Samba" "${HEADER}Samba                      ALLOW       Anywhere                   # temporary"$'\n'
expect "a port with a rule comment is not a profile" "" "${HEADER}80/tcp                     ALLOW       Anywhere                   # web"$'\n'
expect "WireGuard-only and restricted rules are not public" "" "${HEADER}22/tcp on wg0              ALLOW       10.8.0.0/24"$'\n'"OpenSSH                    ALLOW       10.0.0.0/8"$'\n'
expect "IPv6 rows are not listed twice" "Samba" "${HEADER}Samba                      ALLOW       Anywhere"$'\n'"Samba (v6)                 ALLOW       Anywhere (v6)"$'\n'
expect "two profiles, sorted" "Nginx Full,Samba" "${HEADER}Samba                      ALLOW       Anywhere"$'\n'"Nginx Full                 ALLOW       Anywhere"$'\n'
exit "$FAILED"
