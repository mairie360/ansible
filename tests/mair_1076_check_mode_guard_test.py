#!/usr/bin/env python3
"""MAIR-1076: `site.yml --check` stopped on a machine that lacks a package of the
k8s_node role (auditd, ufw, fail2ban are installed by the first task, which a
check run only simulates), so a dry run could not review a machine before its
first real run. Every task that needs one of those packages must be skipped
when the install is only simulated, as the n8n_node role already does.

    python3 tests/mair_1076_check_mode_guard_test.py
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = "not (ansible_check_mode and k8s_node_packages is changed)"
PACKAGE_FILES = ("/etc/fail2ban/",)
PACKAGE_SERVICES = ("auditd", "fail2ban")


def needs_package(task):
    if "community.general.ufw" in task:
        return True
    for module in ("ansible.builtin.service", "ansible.builtin.systemd"):
        args = task.get(module)
        if isinstance(args, dict) and args.get("name") in PACKAGE_SERVICES:
            return True
    copy = task.get("ansible.builtin.copy")
    return isinstance(copy, dict) and str(copy.get("dest", "")).startswith(PACKAGE_FILES)


def conditions(task):
    when = task.get("when", [])
    return [when] if isinstance(when, str) else list(when)


tasks = yaml.safe_load((ROOT / "roles/k8s_node/tasks/main.yml").read_text())
failed = 0

installs = [t for t in tasks if "ansible.builtin.apt" in t]
if not installs or installs[0].get("register") != "k8s_node_packages":
    print("  FAIL  the package install task must register k8s_node_packages")
    failed = 1

checked = 0
for task in tasks:
    if not needs_package(task):
        continue
    checked += 1
    if GUARD not in conditions(task):
        print(f"  FAIL  '{task['name']}' is not skipped when the package install is only simulated")
        failed = 1

if checked < 10:
    print(f"  FAIL  only {checked} package-dependent tasks found, the detection is broken")
    failed = 1

print("FAILED" if failed else f"OK ({checked} tasks guarded)")
sys.exit(failed)
