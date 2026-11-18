#!/usr/bin/env bash
# The release secrets live in the Prod environment (main only) and reach the prod
# promotion alone; pull request runs only get the low-quota key of the AI reviews.
# The workflows are read job by job, so a quoted `inherit`, a key mapped from another
# secret or a release secret read by another job of the same file are caught too.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
python3 - "$root" "ops.yml:pull-request" 1 <<'PY' || exit 1
import os
import re
import subprocess
import sys

RELEASE = re.compile(r"secrets\.(RELEASE_APP_ID|RELEASE_APP_PK|ANTHROPIC_API_KEY)\b")
REVIEW_CALLERS = re.compile(
    r"mairie360/CICD/\.github/workflows/(APIs_cicd|BFFs-cicd|front-libs-cicd|gdpr-pr-cicd|promote-prod)\.yml@"
)


def strip_comment(line):
    return re.sub(r"(^|\s)#.*$", "", line).rstrip()


def parse_jobs(text):
    """Jobs of a workflow: name, `uses`, `secrets` (a scalar or a key -> expression map), own lines."""
    jobs, in_jobs, job, in_secrets = [], False, None, False
    for raw in text.split("\n"):
        line = strip_comment(raw)
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs or not line.strip():
            continue
        if re.match(r"^\S", line):
            in_jobs, job = False, None
            continue
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if header:
            job = {"name": header.group(1), "uses": "", "secrets": None, "lines": []}
            jobs.append(job)
            in_secrets = False
            continue
        if job is None:
            continue
        job["lines"].append(line)
        indent = len(line) - len(line.lstrip(" "))
        if in_secrets and indent <= 4:
            in_secrets = False
        if in_secrets:
            entry = re.match(r"^\s+([A-Za-z0-9_]+):\s*(.+)$", line)
            if entry:
                job["secrets"][entry.group(1)] = entry.group(2).strip()
            continue
        uses = re.match(r"^    uses:\s*(\S+)", line)
        if uses:
            job["uses"] = uses.group(1).strip("'\"")
        secrets = re.match(r"^    secrets:\s*(.*)$", line)
        if secrets:
            value = secrets.group(1).strip().strip("'\"")
            if value:
                job["secrets"] = value
            else:
                job["secrets"], in_secrets = {}, True
    return jobs


def check(root, allowed, gdpr):
    folder = os.path.join(root, ".github", "workflows")
    errors, gdpr_callers = [], 0
    for file in sorted(os.listdir(folder)):
        if not re.search(r"\.ya?ml$", file):
            continue
        text = open(os.path.join(folder, file), encoding="utf-8").read()
        jobs = parse_jobs(text)
        live = "\n".join(strip_comment(l) for l in text.split("\n"))
        in_allowed = 0
        for job in jobs:
            where = f"{file}:{job['name']}"
            if isinstance(job["secrets"], str):
                errors.append(f"{where}: secrets: {job['secrets']}")
            mapping = job["secrets"] if isinstance(job["secrets"], dict) else {}
            for key, expression in mapping.items():
                if re.findall(r"secrets\.([A-Za-z0-9_]+)", expression) != [key]:
                    errors.append(f"{where}: {key} reads {expression}")
            if where in allowed:
                in_allowed += len(RELEASE.findall("\n".join(job["lines"])))
            if REVIEW_CALLERS.search(job["uses"]) and "ANTHROPIC_REVIEW_API_KEY" not in mapping:
                errors.append(f"{where}: no ANTHROPIC_REVIEW_API_KEY")
            if "gdpr-pr-cicd.yml@" in job["uses"]:
                gdpr_callers += 1
        if len(RELEASE.findall(live)) != in_allowed:
            errors.append(f"{file}: release secret read outside {sorted(allowed)}")
    if gdpr and gdpr_callers == 0:
        errors.append("no job calls gdpr-pr-cicd.yml")
    tracked = subprocess.run(["git", "-C", root, "ls-files", "--", "RELEASE_JOURNAL.md"], capture_output=True, text=True).stdout
    if tracked.strip():
        errors.append("RELEASE_JOURNAL.md is tracked")
    return errors
if __name__ == "__main__":
    allowed = set(filter(None, sys.argv[2].split(",")))
    problems = check(sys.argv[1], allowed, sys.argv[3] == "1")
    for problem in problems:
        print(f"MAIR-681: {problem}")
    sys.exit(1 if problems else 0)
PY
echo "MAIR-681: workflows list their secrets, release secrets only in ops.yml:pull-request"
