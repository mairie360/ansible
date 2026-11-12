#!/usr/bin/env bash
# Installed by Ansible (role github_runner) — do not edit on the machine.
# ExecStart of gh-runner.service, run as the runner user: consumes the
# single-use JIT config written by prepare.sh and runs exactly one job. When
# the job ends the runner exits, systemd restarts the service and
# prepare.sh registers a fresh runner.
# Role ops_runner (MAIR-803) sets RUNNER_STATE_DIR and RUNNER_RUN_DIR.
set -euo pipefail
run=${RUNNER_RUN_DIR:-/run/gh-runner}
config=$(cat "$run/jitconfig")
rm -f "$run/jitconfig"
cd "${RUNNER_STATE_DIR:-/var/lib/gh-runner}/runner"
exec ./run.sh --jitconfig "$config"
