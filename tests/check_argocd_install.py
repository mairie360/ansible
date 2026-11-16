#!/usr/bin/env python3
"""MAIR-242: the Argo CD install task must report `changed` only when the cluster differs.

`kubectl apply --server-side` prints "serverside-applied" for every object, changed or not,
so a `changed_when` on its output is either always or never true. The role compares first
(`kubectl diff --server-side`, exit 1 = differences) and applies only then. Also pins the
checksum of the argocd CLI download.

    python3 tests/check_argocd_install.py        (needs PyYAML)
"""
import pathlib
import sys

import yaml

TASKS = pathlib.Path(__file__).resolve().parent.parent / "roles/k8s_argocd/tasks/main.yml"
tasks = {t.get("name"): t for t in yaml.safe_load(TASKS.read_text())}
errors = []

diff = tasks.get("Compare the cluster with the pinned Argo CD manifests")
apply_ = tasks.get("Installer Argo CD (version épinglée)")
cli = tasks.get("Install the argocd CLI (pinned, sha256 verified; used by phase 3)")

if not diff or "kubectl diff" not in diff["ansible.builtin.command"] or "--server-side" not in diff["ansible.builtin.command"]:
    errors.append("missing the server-side `kubectl diff` task before the Argo CD apply")
elif diff.get("failed_when") != "argocd_diff.rc > 1" or diff.get("changed_when") is not False:
    errors.append("the diff task must fail only above exit code 1 and never report changed")
if not apply_:
    errors.append("missing the Argo CD apply task")
else:
    if apply_.get("when") != "argocd_diff.rc == 1":
        errors.append("the apply must run only when the diff found differences (rc == 1)")
    if apply_.get("changed_when") is not True:
        errors.append("an apply that ran changed the cluster: changed_when must be true")
    if "register" in apply_ or "serverside-applied" in str(apply_.get("changed_when")):
        errors.append("do not read the output of `apply --server-side` to detect changes")
if not cli or not str(cli["ansible.builtin.get_url"].get("checksum", "")).startswith("sha256:"):
    errors.append("the argocd CLI download must carry a sha256 checksum")

for e in errors:
    print(f"::error file=roles/k8s_argocd/tasks/main.yml::{e}")
sys.exit(1 if errors else 0)
