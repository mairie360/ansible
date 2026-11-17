#!/usr/bin/env python3
"""Rebuild inventory/hosts.yml from the example and Scaleway (MAIR-800).

Since MAIR-708 the real inventory is gitignored: this repository is public and
must not publish the machines' addresses. On 2026-11-06 nobody had a copy and it
had to be dug out of the git history. The only thing the real file adds to
inventory/hosts.example.yml is the public address of each machine
(`ansible_host`), and Scaleway already knows it: this script copies the example
and fills `ansible_host` with the public IPv4 of the Scaleway server of each
host, read with the `scw` CLI (`scw init` done, project access).

    python3 scripts/restore-inventory.py            # writes inventory/hosts.yml
    python3 scripts/restore-inventory.py --force    # replaces an existing one
    python3 scripts/restore-inventory.py --check    # compares with the existing one

A host is matched with the server named like it, or with its `scw_server` host
variable when the names differ (`mairie360-argocd` is the server "argocd hub").
Hosts of other providers (a client's own machines) are not supported: write
their address by hand. The script refuses to write to a path git tracks or does
not ignore, so the addresses never land in a commit; nothing is printed but host
names. Until MAIR-637 generates the inventory from the Terraform outputs of
mairie360/Infra, this is the way to restore it.

`SCW_BIN` overrides the `scw` command (tests/restore_inventory_test.sh).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST_RE = re.compile(r"^(?P<indent> {8})(?P<name>[A-Za-z0-9_.-]+):\s*$")
VAR_RE = re.compile(r"^ {10}(?P<key>[A-Za-z_][A-Za-z0-9_]*):\s*(?P<value>.*?)\s*$")
ANSIBLE_HOST_RE = re.compile(r"^(?P<prefix> {10}ansible_host:\s*).*$")


def fail(message: str) -> None:
    print(f"restore-inventory: {message}", file=sys.stderr)
    sys.exit(1)


def unquote(value: str) -> str:
    value = value.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def hosts_of(lines: list[str]) -> dict[str, str]:
    """Host name -> Scaleway server name, for every uncommented host of the example."""
    hosts: dict[str, str] = {}
    current = None
    for line in lines:
        match = HOST_RE.match(line)
        if match:
            current = match["name"]
            hosts[current] = current
            continue
        var = VAR_RE.match(line)
        if current and var and var["key"] == "scw_server":
            hosts[current] = unquote(var["value"])
        elif not line.startswith(" " * 10) and line.strip() and not line.lstrip().startswith("#"):
            current = None
    return hosts


def scaleway_addresses() -> dict[str, str]:
    """Server name -> public IPv4, for every server of every zone."""
    command = shlex.split(os.environ.get("SCW_BIN", "scw")) + [
        "instance", "server", "list", "zone=all", "-o", "json",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        fail("the scw CLI is not installed (https://cli.scaleway.com), or set SCW_BIN.")
    if result.returncode != 0:
        fail(f"`scw instance server list` failed ({result.stderr.strip() or result.returncode}). Run `scw init` first.")
    try:
        servers = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        fail("`scw instance server list` did not answer JSON.")
    addresses: dict[str, str] = {}
    duplicates = set()
    for server in servers:
        ips = list(server.get("public_ips") or [])
        if server.get("public_ip"):
            ips.append(server["public_ip"])
        ipv4 = next((ip.get("address") for ip in ips if ip.get("family", "inet") == "inet" and ip.get("address")), None)
        name = server.get("name", "")
        if name in addresses:
            duplicates.add(name)
        if ipv4:
            addresses[name] = ipv4
    for name in duplicates:
        addresses.pop(name, None)
        print(f"restore-inventory: several servers are named '{name}', it is left out.", file=sys.stderr)
    return addresses


def render(lines: list[str], addresses: dict[str, str], hosts: dict[str, str]) -> list[str]:
    missing = [host for host, server in hosts.items() if server not in addresses]
    if missing:
        fail("no Scaleway server with a public IPv4 for: "
             + ", ".join(f"{host} (server '{hosts[host]}')" for host in missing)
             + ". Set its `scw_server` in the example, or write the address by hand.")
    out = []
    current = None
    for line in lines:
        match = HOST_RE.match(line)
        if match:
            current = match["name"]
        elif current in hosts:
            ansible_host = ANSIBLE_HOST_RE.match(line)
            if ansible_host:
                line = f'{ansible_host["prefix"]}"{addresses[hosts[current]]}"\n'
        out.append(line)
    header = [
        "# GENERATED by scripts/restore-inventory.py from inventory/hosts.example.yml\n",
        "# and the public IPv4 of each Scaleway server (MAIR-800). gitignored: never\n",
        "# commit it. Edit the example, then run the script again.\n",
    ]
    return header + out


def ignored_by_git(path: Path) -> bool:
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", str(path)],
                             capture_output=True, check=False).returncode == 0
    ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(path)],
                             capture_output=True, check=False).returncode == 0
    return ignored and not tracked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--example", type=Path, default=ROOT / "inventory/hosts.example.yml")
    parser.add_argument("--out", type=Path, default=ROOT / "inventory/hosts.yml")
    parser.add_argument("--force", action="store_true", help="replace an existing inventory")
    parser.add_argument("--check", action="store_true",
                        help="only compare the addresses of the existing inventory with Scaleway")
    args = parser.parse_args()

    # Before asking Scaleway anything: never write addresses where git would
    # commit them, never replace an inventory by accident.
    if not args.check:
        if not ignored_by_git(args.out):
            fail(f"{args.out} is not ignored by git (or is tracked): refusing to write public addresses there.")
        if args.out.exists() and not args.force:
            fail(f"{args.out} already exists: use --check to compare it, --force to replace it.")

    lines = args.example.read_text(encoding="utf-8").splitlines(keepends=True)
    hosts = hosts_of(lines)
    if not hosts:
        fail(f"no host found in {args.example}.")
    wanted = render(lines, scaleway_addresses(), hosts)

    if args.check:
        if not args.out.exists():
            fail(f"{args.out} does not exist: run the script without --check.")
        current = args.out.read_text(encoding="utf-8").splitlines(keepends=True)
        have = {}
        host = None
        for line in current:
            match = HOST_RE.match(line)
            if match:
                host = match["name"]
            elif host and ANSIBLE_HOST_RE.match(line):
                have[host] = unquote(line.split(":", 1)[1])
        want = {}
        host = None
        for line in wanted:
            match = HOST_RE.match(line)
            if match:
                host = match["name"]
            elif host and ANSIBLE_HOST_RE.match(line):
                want[host] = unquote(line.split(":", 1)[1])
        stale = sorted(h for h in hosts if have.get(h) != want.get(h))
        if stale:
            fail(f"address differs from Scaleway (or is missing) for: {', '.join(stale)}. Re-run with --force.")
        print(f"OK    {len(hosts)} hosts match Scaleway: {', '.join(hosts)}")
        return

    args.out.write_text("".join(wanted), encoding="utf-8")
    print(f"Wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}: "
          f"{len(hosts)} hosts ({', '.join(hosts)}).")


if __name__ == "__main__":
    main()
