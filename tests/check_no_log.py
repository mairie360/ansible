#!/usr/bin/env python3
"""Static check: tasks that handle secrets must not log them.

Run from the repository root (no machine, no network needed):

    python3 tests/check_no_log.py

Fails (exit 1) when a task under roles/ or playbooks/:
  - reads a file with slurp, or asks for hidden input (pause with echo: false),
    or registers/copies a value whose name looks like a secret, without
    `no_log: true`;
  - prints, through `debug`, a variable whose name looks like a secret;
  - templates a file known to embed a secret without `no_log: true`.
"""

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Variable names that hold secret material in this repo.
SECRET_NAME = re.compile(
    r"(password|passwd|token|private|secret|kubeconfig|sealing_key|user_input|wg_private)",
    re.IGNORECASE,
)
# Names that match SECRET_NAME but only hold paths or existence checks.
SAFE_NAMES = {"argocd_initial_secret", "sealing_keys_dir", "local_instance_dir"}
# Files that are secret when read (slurp).
SECRET_FILE = re.compile(r"(private|k3s\.yaml|kube|secret|token|password|sealing)", re.IGNORECASE)
# Templates rendered with a secret inside.
SECRET_TEMPLATES = {"wg0.conf.j2"}

TASK_KEYS = ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers")


def module_of(task):
    for key in task:
        if key.startswith("ansible.builtin.") or key.startswith("community.") or key.startswith("ansible.posix."):
            return key.rsplit(".", 1)[-1], task[key]
    for key in ("slurp", "pause", "debug", "set_fact", "copy", "template", "shell", "command"):
        if key in task:
            return key, task[key]
    return None, None


def iter_tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from iter_tasks(item)
    elif isinstance(node, dict):
        if any(k in node for k in TASK_KEYS):
            for k in TASK_KEYS:
                if k in node:
                    yield from iter_tasks(node[k])
        if "name" in node or module_of(node)[0]:
            yield node


def jinja_vars(value):
    """Root names of the variables a (nested) Jinja value references."""
    text = repr(value)
    names = set()
    for expr in re.findall(r"\{\{(.*?)\}\}", text, re.DOTALL):
        # Drop quoted literals so that words inside strings are not taken as variables.
        expr = re.sub(r"'[^']*'|\"[^\"]*\"", "", expr)
        names.update(re.findall(r"(?<![\w.])([A-Za-z_]\w*)", expr))
    return names


def secret_vars(value, sensitive):
    return sorted(
        v for v in jinja_vars(value)
        if v not in SAFE_NAMES and (SECRET_NAME.search(v) or v in sensitive)
    )


def problems(task, sensitive):
    """Problems of one task. `sensitive` collects the registers holding secrets."""
    name = task.get("name", "<unnamed>")
    no_log = task.get("no_log") is True
    module, args = module_of(task)
    args = args if isinstance(args, dict) else {"_raw": args}
    reg = task.get("register")
    out = []
    if module == "slurp" and SECRET_FILE.search(str(args.get("src", ""))):
        if reg:
            sensitive.add(reg)
        if not no_log:
            out.append(f"slurp of {args.get('src')} without no_log")
    if module == "pause" and args.get("echo") is False:
        if reg:
            sensitive.add(reg)
        if not no_log:
            out.append("hidden prompt without no_log")
    if module == "debug":
        for var in secret_vars(args, sensitive):
            out.append(f"debug prints {var}")
        var = str(args.get("var", "")).split(".")[0]
        if var and var not in SAFE_NAMES and (SECRET_NAME.search(var) or var in sensitive):
            out.append(f"debug prints {var}")
    if module == "set_fact":
        leaked = [k for k, v in args.items() if secret_vars(v, sensitive)]
        sensitive.update(k for k in args if k in leaked or SECRET_NAME.search(k))
        if leaked and not no_log:
            out.append(f"set_fact {', '.join(leaked)} from a secret without no_log")
    if module == "copy" and "content" in args and secret_vars(args["content"], sensitive) and not no_log:
        out.append("copy of a secret content without no_log")
    if module == "template" and pathlib.Path(str(args.get("src", ""))).name in SECRET_TEMPLATES and not no_log:
        out.append("template embedding a secret without no_log")
    if module in ("shell", "command") and reg and SECRET_NAME.search(reg) and reg not in SAFE_NAMES:
        sensitive.add(reg)
        if not no_log:
            out.append(f"registers {reg} without no_log")
    return [f"{name}: {p}" for p in out]


def main():
    failures = []
    files = sorted((ROOT / "roles").glob("*/tasks/*.yml")) + sorted((ROOT / "playbooks").glob("*.yml"))
    for path in files:
        sensitive = set()
        for task in iter_tasks(yaml.safe_load(path.read_text()) or []):
            for p in problems(task, sensitive):
                failures.append(f"{path.relative_to(ROOT)}: {p}")
    for f in failures:
        print(f"FAIL {f}")
    print(f"{len(files)} files checked, {len(failures)} problem(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
