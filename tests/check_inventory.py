#!/usr/bin/env python3
"""Static check: no public IP in clear in the repository (MAIR-415).

Run from the repository root (no machine, no network needed):

    python3 tests/check_inventory.py

Fails (exit 1) when:
  - inventory/group_vars/all/vault.yml is not ansible-vault encrypted;
  - a tracked YAML/Jinja/cfg file holds an IPv4 address outside the private,
    loopback and documentation ranges.
"""

import ipaddress
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VAULT = ROOT / "inventory" / "group_vars" / "all" / "vault.yml"
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
SCANNED = ("*.yml", "*.yaml", "*.j2", "*.cfg", "*.example")
# Dotted numbers that are not addresses (k3s version keys, "1.35.8+k3s1" ->
# "1.35.8.1", in roles/k8s_node).
NOT_ADDRESSES = {"1.35.8.1"}


def main() -> int:
    problems = []

    if not VAULT.exists():
        problems.append(f"{VAULT.relative_to(ROOT)} is missing")
    elif not VAULT.read_text().startswith("$ANSIBLE_VAULT;"):
        problems.append(
            f"{VAULT.relative_to(ROOT)} is not encrypted: "
            "ansible-vault encrypt inventory/group_vars/all/vault.yml"
        )

    for pattern in SCANNED:
        for path in ROOT.rglob(pattern):
            if path == VAULT or ".git" in path.parts or "logs" in path.parts:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for match in IPV4.findall(line):
                    if match in NOT_ADDRESSES:
                        continue
                    try:
                        ip = ipaddress.IPv4Address(match)
                    except ValueError:
                        continue
                    if ip.is_global:
                        problems.append(
                            f"{path.relative_to(ROOT)}:{number}: public IP {ip} "
                            "(move it to the vault, vault_public_ips)"
                        )

    for problem in problems:
        print(f"FAIL {problem}")
    print(f"{len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
