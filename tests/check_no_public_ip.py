#!/usr/bin/env python3
"""Static check (MAIR-708): no tracked file may hold a public IPv4 address.

This repository is public: the real inventory, with the machines' public
addresses, stays in the gitignored inventory/hosts.yml (template:
inventory/hosts.example.yml). Private ranges (RFC 1918, loopback, link-local,
CGNAT), the documentation ranges (RFC 5737) and CIDR network addresses of
private ranges are allowed; multicast
groups are no machine address and are skipped.

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
# Four dotted numbers that are not part of a longer dotted number: an address ending a sentence
# ("... 192.0.2.1.") or glued to a word ("host_192.0.2.1") still matches.
IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")


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
            # Multicast groups (224.0.0.0/4) are "global" for ipaddress but are no machine address.
            if address.is_global and not address.is_multicast:
                yield line_number, match.group(0)


def main():
    findings = []
    for path in tracked_files():
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary file
        # latin-1 decodes any byte: a text file that is not valid UTF-8 is still scanned.
        for line_number, address in public_addresses(data.decode("latin-1")):
            findings.append(f"{path.relative_to(ROOT)}:{line_number}: public IP {address}")
    if findings:
        print("Public IP addresses in tracked files (MAIR-708): keep them in the gitignored inventory/hosts.yml")
        print("\n".join(findings))
        return 1
    print("No public IP address in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
