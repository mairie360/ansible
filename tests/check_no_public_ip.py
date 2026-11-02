#!/usr/bin/env python3
"""Static check (MAIR-708): no tracked file may hold a public IPv4 address.

This repository is public: the real inventory, with the machines' public
addresses, stays in the gitignored inventory/hosts.yml (template:
inventory/hosts.example.yml). Private ranges (RFC 1918, loopback, link-local,
CGNAT), the documentation ranges (RFC 5737) and CIDR network addresses of
private ranges are allowed.

Run from the repository root (no machine, no network needed):

    python3 tests/check_no_public_ip.py

Exits 1 and lists file:line for every public address found.
"""

import ipaddress
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# Dotted strings that look like addresses but are not, with the reason.
NOT_ADDRESSES = {
    ("roles/k8s_node/tasks/main.yml", "1.35.8.1"): "k3s version normalized for the `version` test",
}


def tracked_files():
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [ROOT / name for name in listing.decode().split("\0") if name]


def public_addresses(text):
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in IPV4.finditer(line):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue  # not an address (e.g. 1.2.300.4)
            if address.is_global:
                yield line_number, match.group(0)


def main():
    findings = []
    for path in tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary file
        for line_number, address in public_addresses(text):
            if (str(path.relative_to(ROOT)), address) in NOT_ADDRESSES:
                continue
            findings.append(f"{path.relative_to(ROOT)}:{line_number}: public IP {address}")
    if findings:
        print("Public IP addresses in tracked files (MAIR-708): keep them in the gitignored inventory/hosts.yml")
        print("\n".join(findings))
        return 1
    print("No public IP address in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
