"""MAIR-637: inventory/hosts.yml is built from the Terraform outputs of mairie360/Infra.

python -m unittest tests/test_inventory_from_terraform.py -v

No machine nor Terraform needed: the Terraform output is a fixture shaped like
the `ansible_hosts` output of Infra (orgs/<org>/outputs.tf), with documentation
addresses (RFC 5737).
"""

import importlib.util
import json
import pathlib
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("inv", REPO / "scripts/inventory-from-terraform.py")
inv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inv)

TERRAFORM = {
    "mairie360-argocd": {"group": "mairie360_argocd", "env_name": None, "public_ipv4": "192.0.2.1"},
    "mairie360-dev": {"group": "mairie360_instances", "env_name": "dev", "public_ipv4": "192.0.2.11"},
    "mairie360-staging": {"group": "mairie360_instances", "env_name": "staging", "public_ipv4": "192.0.2.12"},
    "mairie360-prod": {"group": "mairie360_instances", "env_name": "prod", "public_ipv4": "192.0.2.13"},
}


class InventoryFromTerraform(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.hosts = self.dir / "hosts.yml"
        self.tf = self.dir / "tf.json"
        self.tf.write_text(json.dumps(TERRAFORM))

    def run_script(self, *extra):
        return inv.main([str(self.tf), "--org", "mairie360", "--hosts", str(self.hosts), *extra])

    def load(self):
        return yaml.safe_load(self.hosts.read_text())["all"]

    def test_public_addresses_come_from_terraform_the_rest_from_the_ci_inventory(self):
        self.assertEqual(self.run_script(), 0)
        data = self.load()
        prod = data["children"]["mairie360_instances"]["hosts"]["mairie360-prod"]
        self.assertEqual(prod["ansible_host"], "192.0.2.13")
        self.assertEqual(prod["wg_ip"], "10.10.0.13")
        self.assertEqual(prod["instance_domain"], "prod.mairie360-eip.fr")
        self.assertEqual(prod["ingress_controller"], "traefik")
        argocd = data["children"]["mairie360_argocd"]["hosts"]["mairie360-argocd"]
        self.assertEqual(argocd, {"ansible_host": "192.0.2.1", "wg_ip": "10.10.0.1"})
        self.assertEqual(data["vars"]["ansible_user"], "ubuntu")
        self.assertEqual(data["vars"]["ansible_ssh_private_key_file"], "~/.ssh/login_scaleway_mairie360")
        self.assertEqual(self.hosts.stat().st_mode & 0o777, 0o600)

    def test_ci_only_settings_never_reach_the_workstation_inventory(self):
        self.run_script()
        for group in self.load()["children"].values():
            for host in group["hosts"].values():
                self.assertNotIn("ansible_connection", host)
                self.assertNotEqual(host["ansible_host"], host.get("wg_ip"))

    def test_other_groups_vars_and_extra_host_keys_are_kept(self):
        self.hosts.write_text(yaml.safe_dump({"all": {
            "children": {
                "client_x_argocd": {"hosts": {"client-x-argocd": {"ansible_host": "198.51.100.1"}}},
                "mairie360_instances": {"hosts": {
                    "mairie360-dev": {"ansible_host": "203.0.113.99", "vault_project_id": "p"},
                    "mairie360-old": {"ansible_host": "203.0.113.98"},
                }},
            },
            "vars": {"ansible_user": "me", "ansible_ssh_private_key_file": "~/.ssh/mine"},
        }}))
        self.assertEqual(self.run_script(), 0)
        data = self.load()
        self.assertIn("client_x_argocd", data["children"])
        self.assertEqual(data["vars"]["ansible_user"], "me")
        hosts = data["children"]["mairie360_instances"]["hosts"]
        self.assertEqual(hosts["mairie360-dev"]["ansible_host"], "192.0.2.11", "Terraform wins over a stale address")
        self.assertEqual(hosts["mairie360-dev"]["vault_project_id"], "p")
        self.assertNotIn("mairie360-old", hosts, "a machine gone from Terraform leaves the inventory")

    def test_refuses_when_terraform_and_the_ci_inventory_disagree(self):
        tf = dict(TERRAFORM)
        del tf["mairie360-staging"]
        self.tf.write_text(json.dumps(tf))
        self.assertEqual(self.run_script(), 1)
        self.assertFalse(self.hosts.exists(), "nothing written on a mismatch")

    def test_refuses_a_host_without_public_address(self):
        tf = json.loads(json.dumps(TERRAFORM))
        tf["mairie360-dev"]["public_ipv4"] = None
        self.tf.write_text(json.dumps(tf))
        self.assertEqual(self.run_script(), 1)

    def test_accepts_the_full_terraform_output_document(self):
        self.tf.write_text(json.dumps({"ansible_hosts": {"sensitive": False, "value": TERRAFORM}}))
        self.assertEqual(self.run_script(), 0)

    def test_header_tells_the_file_is_generated(self):
        self.run_script()
        self.assertIn("inventory-from-terraform.py", "\n".join(self.hosts.read_text().splitlines()[:6]))


if __name__ == "__main__":
    unittest.main()
