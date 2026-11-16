#!/usr/bin/env python3
"""Write the machines of one group into inventory/hosts.yml from Terraform (MAIR-637).

The public addresses of the machines come from the `ansible_hosts` output of
mairie360/Infra (orgs/<org>/), never from a hand-typed or committed file.
Everything else about a host (wg_ip, env_name, instance_domain,
ingress_controller) is taken from the tracked inventory/ci-<org>.yml, so the
two inventories cannot drift:

    # in mairie360/Infra
    scripts/tf.sh orgs/mairie360 output -json ansible_hosts > /tmp/hosts.json
    # here
    scripts/inventory-from-terraform.py --org mairie360 /tmp/hosts.json

Only the two groups of that org (<org>_argocd, <org>_instances) are rewritten;
other groups, top-level vars and extra per-host keys already in hosts.yml are
kept (YAML comments are not). The run fails, writing nothing, when Terraform
and inventory/ci-<org>.yml do not list the same hosts or a host has no public
address. hosts.yml stays gitignored (MAIR-708).
"""

import argparse
import ipaddress
import json
import pathlib
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
# Keys of a CI inventory host that only make sense on the Argo CD machine (the
# CI controller): never copied into the workstation inventory.
CI_ONLY_KEYS = {"ansible_connection", "ansible_host", "ansible_user", "ansible_ssh_private_key_file"}
HEADER = """\
# =============================================================================
# Real inventory: GITIGNORED (MAIR-708), it holds the machines' public addresses.
# The groups of each org listed below are written by
# scripts/inventory-from-terraform.py (MAIR-637) from the Terraform outputs of
# mairie360/Infra and inventory/ci-<org>.yml: re-run it instead of editing them.
# Generated groups: {groups}
# =============================================================================
"""


class InventoryError(Exception):
    pass


def load_terraform(raw):
    data = json.loads(raw)
    # `terraform output -json` (all outputs) wraps each one; `output -json ansible_hosts` does not.
    if "ansible_hosts" in data:
        data = data["ansible_hosts"]
        data = data.get("value", data)
    return data


def build(org, terraform_hosts, ci_inventory, current, user, key):
    group = org.replace("-", "_")
    names = (f"{group}_argocd", f"{group}_instances")
    ci_children = ci_inventory["all"]["children"]
    ci_hosts = {name: dict(host or {}) for g in names for name, host in ci_children[g]["hosts"].items()}

    if set(ci_hosts) != set(terraform_hosts):
        raise InventoryError(
            f"Terraform and inventory/ci-{org}.yml disagree: only in Terraform "
            f"{sorted(set(terraform_hosts) - set(ci_hosts))}, only in the CI inventory "
            f"{sorted(set(ci_hosts) - set(terraform_hosts))}"
        )

    inventory = current or {}
    root = inventory.setdefault("all", {})
    children = root.setdefault("children", {}) or {}
    root["children"] = children
    variables = root.setdefault("vars", {}) or {}
    root["vars"] = variables
    variables.setdefault("ansible_user", user)
    variables.setdefault("ansible_ssh_private_key_file", key)

    for g in names:
        old_hosts = ((children.get(g) or {}).get("hosts") or {})
        new_hosts = {}
        for name in ci_children[g]["hosts"]:
            tf = terraform_hosts[name]
            if tf.get("group") != g:
                raise InventoryError(f"{name}: Terraform puts it in {tf.get('group')}, the CI inventory in {g}")
            address = tf.get("public_ipv4")
            if not address:
                raise InventoryError(f"{name}: Terraform gives no public IPv4")
            ipaddress.IPv4Address(address)
            host = dict(old_hosts.get(name) or {})
            host.update({k: v for k, v in ci_hosts[name].items() if k not in CI_ONLY_KEYS})
            host["ansible_host"] = address
            if tf.get("env_name") and host.get("env_name") != tf["env_name"]:
                raise InventoryError(f"{name}: env_name {host.get('env_name')} != Terraform's {tf['env_name']}")
            # ansible_host first: it is what a reader looks for.
            new_hosts[name] = {"ansible_host": address, **{k: v for k, v in host.items() if k != "ansible_host"}}
        children[g] = {**(children.get(g) or {}), "hosts": new_hosts}
    return inventory


def generated_groups(inventory):
    return sorted(
        g[: -len("_argocd")] for g in (inventory["all"].get("children") or {}) if g.endswith("_argocd")
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("terraform_output", help="JSON of `terraform output -json ansible_hosts` ('-' = stdin)")
    parser.add_argument("--org", required=True, help="group id, e.g. mairie360")
    parser.add_argument("--hosts", type=pathlib.Path, default=REPO / "inventory/hosts.yml")
    parser.add_argument("--ci-inventory", type=pathlib.Path, help="default: inventory/ci-<org>.yml")
    parser.add_argument("--user", default="ubuntu", help="ansible_user when hosts.yml has none yet")
    parser.add_argument("--key", help="ansible_ssh_private_key_file when hosts.yml has none yet "
                                      "(default ~/.ssh/login_scaleway_<org>)")
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.terraform_output == "-" else pathlib.Path(args.terraform_output).read_text()
    ci_path = args.ci_inventory or REPO / f"inventory/ci-{args.org}.yml"
    current = yaml.safe_load(args.hosts.read_text()) if args.hosts.exists() else None
    try:
        inventory = build(
            args.org,
            load_terraform(raw),
            yaml.safe_load(ci_path.read_text()),
            current,
            args.user,
            args.key or f"~/.ssh/login_scaleway_{args.org}",
        )
    except (InventoryError, KeyError, ValueError) as error:
        print(f"inventory-from-terraform: {error}", file=sys.stderr)
        return 1

    text = HEADER.format(groups=", ".join(generated_groups(inventory)))
    text += yaml.safe_dump(inventory, sort_keys=False, default_flow_style=False)
    tmp = args.hosts.with_suffix(".yml.tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(args.hosts)
    print(f"{args.hosts}: {args.org} hosts written from Terraform")
    return 0


if __name__ == "__main__":
    sys.exit(main())
