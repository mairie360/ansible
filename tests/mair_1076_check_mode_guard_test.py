#!/usr/bin/env python3
"""MAIR-1076: `site.yml --check` stopped on a machine that lacks a package of the
k8s_node role (auditd, ufw, fail2ban), because their install is only simulated
in a check run. Every task that needs one of those packages must be skipped
when that install is simulated, as the n8n_node role already does.

Static check, needs PyYAML:

    python3 tests/mair_1076_check_mode_guard_test.py
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = "not (ansible_check_mode and k8s_node_packages is changed)"
PACKAGES = {"ufw", "fail2ban", "auditd"}
PACKAGE_SERVICES = {"auditd", "fail2ban"}
PACKAGE_DIRS = ("/etc/fail2ban/",)
BLOCK_KEYS = ("block", "rescue", "always")


def conditions(task):
    when = task.get("when", [])
    return [when] if isinstance(when, str) else list(when)


def walk(tasks, inherited=()):
    """Yield (task, conditions of the task and of its enclosing blocks)."""
    for task in tasks:
        own = list(inherited) + conditions(task)
        yield task, own
        for key in BLOCK_KEYS:
            yield from walk(task.get(key, []), own)


def needs_package(task):
    if "community.general.ufw" in task:
        return True
    for module in ("ansible.builtin.service", "ansible.builtin.systemd"):
        args = task.get(module)
        if isinstance(args, dict) and args.get("name") in PACKAGE_SERVICES:
            return True
    copy = task.get("ansible.builtin.copy")
    return isinstance(copy, dict) and str(copy.get("dest", "")).startswith(PACKAGE_DIRS)


def package_names(task):
    args = task.get("ansible.builtin.apt")
    names = args.get("name", []) if isinstance(args, dict) else []
    return {names} if isinstance(names, str) else set(names)


tasks = yaml.safe_load((ROOT / "roles/k8s_node/tasks/main.yml").read_text())
failed = 0

# The install that owns the packages is the one that registers the result.
owners = [t for t, _ in walk(tasks) if PACKAGES <= package_names(t)]
if len(owners) != 1 or owners[0].get("register") != "k8s_node_packages":
    print("  FAIL  one apt task installing ufw, fail2ban and auditd must register k8s_node_packages")
    failed = 1

# A check run must read the real k3s version: skipped, it looks like k3s is absent.
reads = [t for t, _ in walk(tasks) if t.get("name") == "Read the installed k3s version"]
if len(reads) != 1 or reads[0].get("check_mode") is not False:
    print("  FAIL  'Read the installed k3s version' must set check_mode: false")
    failed = 1

checked = 0
for task, conds in walk(tasks):
    if not needs_package(task):
        continue
    checked += 1
    if GUARD not in conds:
        print(f"  FAIL  '{task['name']}' is not skipped when the package install is only simulated")
        failed = 1

if checked < 10:
    print(f"  FAIL  only {checked} package-dependent tasks found, the detection is broken")
    failed = 1

print("FAILED" if failed else f"OK ({checked} tasks guarded)")
sys.exit(failed)
