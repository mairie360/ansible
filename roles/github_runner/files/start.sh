#!/usr/bin/env bash
# Installed by Ansible (role github_runner) — do not edit on the machine.
# ExecStart of gh-runner.service, run as the runner user: consumes the
# single-use JIT config written by prepare.sh and runs exactly one job. When
# the job ends the runner exits, systemd restarts the service and
# prepare.sh registers a fresh runner.
set -euo pipefail
config=$(cat /run/gh-runner/jitconfig)
rm -f /run/gh-runner/jitconfig
cd /var/lib/gh-runner/runner
exec ./run.sh --jitconfig "$config"
