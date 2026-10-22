#!/usr/bin/env python3
"""Static check of the GDPR settings of the roles (MAIR-293), no machine needed:

    python3 tests/check_gdpr_config.py

Fails (exit 1) when the Hubble metrics label a flow with an IP address (labelsContext
source_ip / destination_ip): Hubble's metrics would then keep, per client pod, who called what.
"""
import pathlib
import re
import sys

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "roles/k8s_node/templates/cilium-values.yaml.j2"


def main():
    problems = []
    for number, line in enumerate(TEMPLATE.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        context = re.search(r"labelsContext=([^;\"]*)", line)
        if context:
            ips = [label for label in context.group(1).split(",") if label.endswith("_ip")]
            if ips:
                problems.append(f"{TEMPLATE.name}:{number}: Hubble metrics labelled with {', '.join(ips)}")
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"{len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
