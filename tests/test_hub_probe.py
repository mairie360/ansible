"""External probe (MAIR-621): python3 -m unittest tests/test_hub_probe.py -v

- filter hub_probe_urls builds the probed URLs from the inventory;
- site.yml runs the role on the Argo CD machines only, after the instances;
- the role reads its token from the vault, never from a variable or the manifest.
"""

import importlib.util
import pathlib
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("probe", REPO / "roles/hub_probe/filter_plugins/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeUrls(unittest.TestCase):
    def test_one_url_per_instance_and_host(self):
        self.assertEqual(
            probe.hub_probe_urls(["prod.mairie360-eip.fr", "dev.mairie360-eip.fr"], ["login"]),
            ["https://login.dev.mairie360-eip.fr/", "https://login.prod.mairie360-eip.fr/"],
        )

    def test_refuses_what_is_not_a_domain_or_a_host(self):
        for domains, hosts in ((["dev.mairie360-eip.fr/x"], ["login"]), ([None], ["login"]),
                               (["dev.mairie360-eip.fr"], ["login/"]), (["203.0.113.1"], ["login"])):
            with self.assertRaises(ValueError, msg=(domains, hosts)):
                probe.hub_probe_urls(domains, hosts)


class Wiring(unittest.TestCase):
    def test_site_runs_the_probe_on_the_argocd_machines(self):
        plays = yaml.safe_load((REPO / "playbooks/site.yml").read_text())
        play = next(p for p in plays if any(
            (r.get("role") if isinstance(r, dict) else r) == "hub_probe" for r in p.get("roles", [])))
        self.assertEqual(play["hosts"], "*_argocd", "a probe on an instance would die with it")

    def test_disabled_without_endpoint(self):
        defaults = yaml.safe_load((REPO / "roles/hub_probe/defaults/main.yml").read_text())
        self.assertEqual(defaults["hub_probe_metrics_endpoint"], "")
        self.assertIn("hub_probe_metrics_endpoint | length > 0", defaults["hub_probe_enabled"])
        self.assertEqual(defaults["hub_probe_vault_path"], "/{{ org_id }}/hub",
                         "path of mairie360/Infra orgs/<org>/probes.tf")

    def test_token_only_from_the_vault_and_never_logged(self):
        tasks = yaml.safe_load((REPO / "roles/hub_probe/tasks/main.yml").read_text())
        secret = next(t for t in tasks if t["name"] == "Store the probe's token")
        self.assertTrue(secret.get("no_log"))
        self.assertIn("vault_values.cockpit_token", secret["args"]["stdin"])
        template = (REPO / "roles/hub_probe/templates/probe.yaml.j2").read_text()
        self.assertNotIn("cockpit_token", template)
        self.assertIn("${env:COCKPIT_TOKEN}", template)


if __name__ == "__main__":
    unittest.main()


class Mairie360Group(unittest.TestCase):
    def test_mair_621_the_probe_is_enabled_on_the_mairie360_group(self):
        """Left empty after MAIR-621, the probe never ran: ArgoCDMetricsSilent and
        ProbeSilent fired in Cockpit and no site was watched from outside."""
        group = yaml.safe_load((REPO / "inventory/group_vars/mairie360_argocd.yml").read_text())
        self.assertRegex(
            group["hub_probe_metrics_endpoint"],
            r"^https://[0-9a-f-]{36}\.metrics\.cockpit\.fr-par\.scw\.cloud/otlp/v1/metrics$",
            "OTLP push URL of the mairie360-probes source, same form as the instances' collectors")
