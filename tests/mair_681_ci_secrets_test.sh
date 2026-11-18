#!/usr/bin/env bash
# The release secrets live in the Prod environment (main only) and reach the prod
# promotion alone; pull request runs only get the low-quota key of the AI reviews.
# The workflows are read job by job (quoted keys included): every call of a mairie360/CICD
# workflow passes exactly the secrets that workflow reads, each as a direct
# `${{ secrets.<same name> }}`, and no other job reads a release secret, whatever the notation.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
python3 - "$root" "ops.yml:pull-request" 1 <<'PY' || exit 1
import os
import re
import subprocess
import sys

RELEASE_SECRETS = {"RELEASE_APP_ID", "RELEASE_APP_PK", "ANTHROPIC_API_KEY"}
SECRET_READ = re.compile(r"secrets\s*(?:\.\s*([A-Za-z0-9_]+)|\[\s*['\"]([A-Za-z0-9_]+)['\"]\s*\])", re.IGNORECASE)
REVIEW = ["ANTHROPIC_REVIEW_API_KEY", "ANTHROPIC_WORKSPACE_ID"]
BUILD = ["CODECOV_TOKEN", "N8N_WEBHOOK_SECRET"]
RELEASE_APP = ["RELEASE_APP_ID", "RELEASE_APP_PK"]
# Secrets each mairie360/CICD reusable workflow reads, by called workflow (`[dry-run]`: nightly gates).
EXPECTED = {
    "APIs_cicd.yml": REVIEW + BUILD,
    "BFFs-cicd.yml": REVIEW + BUILD,
    "front-libs-cicd.yml": REVIEW + BUILD,
    "back-lib-cicd.yml": ["CARGO_REGISTRY_TOKEN"] + BUILD + RELEASE_APP,
    "bffs-lib-cicd.yml": BUILD,
    "database_cicd.yml": BUILD,
    "frontend-cicd.yml": BUILD,
    "frontend-dev-exception.yml": [],
    "gdpr-pr-cicd.yml": REVIEW,
    "promote-prod.yml": ["ANTHROPIC_API_KEY", "N8N_WEBHOOK_SECRET"] + REVIEW + RELEASE_APP,
    "promote-prod.yml [dry-run]": ["N8N_WEBHOOK_SECRET"] + REVIEW,
    "promote-staging.yml": ["N8N_WEBHOOK_SECRET"],
    "promote-staging.yml [dry-run]": ["N8N_WEBHOOK_SECRET"],
    "terraform_cicd.yml": ["N8N_WEBHOOK_SECRET"],
}
# The nightly prod dry run only hands the review key over when it asks for the AI review.
DRY_RUN_REVIEW = "${{ needs.check.outputs.ai == 'true' && secrets.ANTHROPIC_REVIEW_API_KEY || '' }}"
CICD_WORKFLOW = re.compile(r"^mairie360/CICD/\.github/workflows/([^@]+)@")


def strip_comment(line):
    return re.sub(r"(^|\s)#.*$", "", line).rstrip()


def unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def key_value(line):
    """`key: value` of a block mapping line, the key unquoted; None for another line."""
    match = re.match(r"""^\s*("[^"]*"|'[^']*'|[^\s:'"][^:]*?)\s*:(?:\s+(.*))?$""", line)
    return (unquote(match.group(1)), (match.group(2) or "").strip()) if match else None


def parse_jobs(text):
    """Jobs of a workflow: name, `uses`, `secrets` (a scalar or a key -> value map), own lines."""
    jobs, in_jobs, job, in_secrets = [], False, None, False
    for raw in text.split("\n"):
        line = strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        entry = key_value(line)
        if indent == 0:
            in_jobs, job = bool(entry) and entry[0] == "jobs" and not entry[1], None
            continue
        if not in_jobs:
            continue
        if indent == 2 and entry and not entry[1]:
            job = {"name": entry[0], "uses": "", "secrets": None, "lines": []}
            jobs.append(job)
            in_secrets = False
            continue
        if job is None:
            continue
        job["lines"].append(line)
        if in_secrets and indent <= 4:
            in_secrets = False
        if in_secrets:
            if entry:
                job["secrets"][entry[0]] = unquote(entry[1])
            continue
        if indent == 4 and entry and entry[0] == "uses":
            job["uses"] = unquote(entry[1])
        if indent == 4 and entry and entry[0] == "secrets":
            value = unquote(entry[1])
            job["secrets"] = value if value else {}
            in_secrets = not value
    return jobs


def release_reads(text):
    return sum(1 for m in SECRET_READ.finditer(text) if (m.group(1) or m.group(2)).upper() in RELEASE_SECRETS)


def check(root, allowed, gdpr):
    folder = os.path.join(root, ".github", "workflows")
    errors, gdpr_callers = [], 0
    for file in sorted(os.listdir(folder)):
        if not re.search(r"\.ya?ml$", file):
            continue
        text = open(os.path.join(folder, file), encoding="utf-8").read()
        jobs = parse_jobs(text)
        live = "\n".join(strip_comment(l) for l in text.split("\n"))
        if re.search(r"^['\"]?jobs['\"]?\s*:", live, re.M) and not jobs:
            errors.append(f"{file}: no job could be read")
        in_allowed = 0
        for job in jobs:
            where = f"{file}:{job['name']}"
            if isinstance(job["secrets"], str):
                errors.append(f"{where}: secrets: {job['secrets']} (list them as a block mapping)")
            mapping = job["secrets"] if isinstance(job["secrets"], dict) else {}
            if where in allowed:
                in_allowed += release_reads("\n".join(job["lines"]))
            called = CICD_WORKFLOW.match(job["uses"])
            if not called:
                continue
            callee = called.group(1) + (" [dry-run]" if file == "nightly-gates.yml" else "")
            if callee not in EXPECTED:
                errors.append(f"{where}: no expected secret list for {callee}")
                continue
            if sorted(mapping) != sorted(EXPECTED[callee]):
                errors.append(f"{where}: passes {sorted(mapping)}, expected {sorted(EXPECTED[callee])}")
            for key, value in mapping.items():
                wanted = DRY_RUN_REVIEW if callee == "promote-prod.yml [dry-run]" and key == "ANTHROPIC_REVIEW_API_KEY" else "${{ secrets.%s }}" % key
                if value != wanted:
                    errors.append(f"{where}: {key}: {value}, expected {wanted}")
            if called.group(1) == "gdpr-pr-cicd.yml":
                gdpr_callers += 1
        if release_reads(live) != in_allowed:
            errors.append(f"{file}: release secret read outside {sorted(allowed)}")
    if gdpr and gdpr_callers == 0:
        errors.append("no job calls gdpr-pr-cicd.yml")
    tracked = subprocess.run(["git", "-C", root, "ls-files", "--", "RELEASE_JOURNAL.md"], capture_output=True, text=True).stdout
    if tracked.strip():
        errors.append("RELEASE_JOURNAL.md is tracked")
    return errors


if __name__ == "__main__":
    problems = check(sys.argv[1], set(filter(None, sys.argv[2].split(","))), sys.argv[3] == "1")
    for problem in problems:
        print(f"MAIR-681: {problem}")
    sys.exit(1 if problems else 0)
PY
echo "MAIR-681: workflows list their secrets, release secrets only in ops.yml:pull-request"
