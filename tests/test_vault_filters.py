#!/usr/bin/env python3
"""Non-regression tests of MAIR-617: what the role writes to the vault.

Unit tests of roles/k8s_instance_secrets/filter_plugins/vault.py, no network,
no cluster:

    python3 -m unittest tests/test_vault_filters.py
"""

import base64
import importlib.util
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "vault_filters", ROOT / "roles/k8s_instance_secrets/filter_plugins/vault.py"
)
vault = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vault)

ENTRIES = {"restic_password": "restic-password", "admin_password": "admin-password", "sealing_keys": "sealing-keys"}


def b64(text):
    return base64.b64encode(text.encode()).decode()


def slurped(path, content):
    return {"source": path, "content": b64(content)}


def key(name, crt="Y3J0", tls_key="a2V5"):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": "kube-system",
            "labels": {"sealedsecrets.bitnami.com/sealed-secrets-key": "active"},
            "uid": "cluster-assigned",
            "resourceVersion": "42",
            "creationTimestamp": "2026-10-01T00:00:00Z",
        },
        "type": "kubernetes.io/tls",
        "data": {"tls.crt": crt, "tls.key": tls_key},
    }


class VaultIds(unittest.TestCase):
    def test_keeps_only_the_instance_path_and_known_names(self):
        listed = [
            {"id": "1", "name": "restic-password", "path": "/mairie360/dev"},
            {"id": "2", "name": "restic-password", "path": "/mairie360/prod"},
            {"id": "3", "name": "other", "path": "/mairie360/dev"},
            {"id": "4", "name": "sealing-keys", "path": "/mairie360/dev"},
        ]
        ids = vault.vault_ids(listed, "/mairie360/dev", ENTRIES.values())
        self.assertEqual(ids, {"restic-password": "1", "sealing-keys": "4"})

    def test_missing_secrets_are_reported_sorted(self):
        self.assertEqual(
            vault.vault_missing({"sealing-keys": "4"}, ENTRIES.values()),
            ["admin-password", "restic-password"],
        )
        self.assertEqual(vault.vault_missing({}, []), [])


class LegacyFiles(unittest.TestCase):
    PREFIXES = {"restic_password": "restic", "admin_password": "admin"}

    def test_sorts_plain_and_suffixed_copies_of_the_instance_only(self):
        files = [
            slurped("/home/op/.mairie360/restic-mairie360-dev.txt", "r1\n"),
            slurped("/home/op/.mairie360/restic-mairie360-dev.txt.123.2026-10-20~", "r0\n"),
            slurped("/home/op/.mairie360/admin-mairie360-dev.txt.from-argocd", "a0\n"),
            slurped("/home/op/.mairie360/admin-mairie360-devx.txt", "other instance"),
            {"skipped": True},
        ]
        out = vault.vault_legacy_files(files, self.PREFIXES, "mairie360", "dev", "workstation")
        self.assertEqual(out["exact"], {"restic_password": "r1"})
        self.assertEqual(
            [(c["key"], c["content"]) for c in out["candidates"]],
            [("restic_password", "r1"), ("restic_password", "r0"), ("admin_password", "a0")],
        )
        self.assertTrue(out["candidates"][1]["source"].startswith("workstation:/home/op/"))


class GeneratedWrites(unittest.TestCase):
    def plan(self, current, finals, candidates=()):
        return vault.vault_generated_writes(current, finals, list(candidates), ENTRIES, "sealed on mairie360/dev")

    def test_nothing_written_when_the_vault_already_holds_the_values(self):
        writes = self.plan({"restic_password": "r", "admin_password": "a"}, {"restic_password": "r", "admin_password": "a"})
        self.assertEqual(writes, [])

    def test_new_value_written_when_the_vault_is_empty(self):
        writes = self.plan({"restic_password": "", "admin_password": ""}, {"restic_password": "r", "admin_password": "a"})
        self.assertEqual(
            [(w["entry"], w["content"]) for w in writes],
            [("admin-password", "a"), ("restic-password", "r")],
        )

    def test_admin_hash_on_the_instance_writes_nothing(self):
        # resolve_generated.yml leaves admin_password out of the writes then.
        writes = self.plan({"restic_password": "r", "admin_password": "a"}, {"restic_password": "r"})
        self.assertEqual(writes, [])

    def test_legacy_copies_are_archived_before_the_final_value(self):
        candidates = [
            {"key": "restic_password", "content": "old", "source": "workstation:restic.txt~"},
            {"key": "restic_password", "content": "r", "source": "workstation:restic.txt"},
            {"key": "restic_password", "content": "old", "source": "workstation:restic.txt.from-x"},
        ]
        writes = self.plan({"restic_password": "", "admin_password": ""}, {"restic_password": "r"}, candidates)
        self.assertEqual([w["content"] for w in writes], ["old", "r"])
        self.assertIn("archived workstation:restic.txt~", writes[0]["description"])
        self.assertEqual(writes[-1]["description"], "MAIR-617 sealed on mairie360/dev")

    def test_latest_is_written_again_after_archiving(self):
        # The vault already holds the value kept; an older copy is archived,
        # then the value is written again so that it stays the latest.
        candidates = [{"key": "admin_password", "content": "leftover", "source": "argocd:admin.txt.2026"}]
        writes = self.plan({"restic_password": "r", "admin_password": "a"}, {"restic_password": "r"}, candidates)
        self.assertEqual([(w["entry"], w["content"]) for w in writes], [("admin-password", "leftover"), ("admin-password", "a")])

    def test_instance_value_replaces_a_different_vault_value_and_keeps_it_as_history(self):
        # The instance wins: the vault's old value stays as the previous version.
        writes = self.plan({"restic_password": "vault", "admin_password": ""}, {"restic_password": "cluster"})
        self.assertEqual([(w["entry"], w["content"]) for w in writes], [("restic-password", "cluster")])


class SealingKeyWrites(unittest.TestCase):
    def test_one_version_per_key_not_stored_yet(self):
        writes = vault.vault_sealing_key_writes(
            [key("sealed-secrets-keyb"), key("sealed-secrets-keya")], [], ["sealed-secrets-keya"], "sealing-keys"
        )
        self.assertEqual([w["description"] for w in writes], ["sealed-secrets-keyb"])
        self.assertEqual(writes[0]["entry"], "sealing-keys")

    def test_second_run_writes_nothing(self):
        keys = [key("sealed-secrets-keya"), key("sealed-secrets-keyb")]
        first = vault.vault_sealing_key_writes(keys, [], [], "sealing-keys")
        second = vault.vault_sealing_key_writes(keys, [], [w["description"] for w in first], "sealing-keys")
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])

    def test_keys_only_in_the_legacy_backup_are_uploaded_once(self):
        writes = vault.vault_sealing_key_writes(
            [key("sealed-secrets-keyb")], [key("sealed-secrets-keya"), key("sealed-secrets-keyb", crt="b2xk")], [], "sealing-keys"
        )
        self.assertEqual([w["description"] for w in writes], ["sealed-secrets-keyb", "sealed-secrets-keya"])
        # The instance's copy wins over the legacy one of the same name.
        self.assertIn("Y3J0", writes[0]["content"])

    def test_manifest_restores_the_key_without_cluster_fields(self):
        manifest = yaml.safe_load(vault.vault_sealing_key_writes([key("k")], [], [], "sealing-keys")[0]["content"])
        self.assertEqual(manifest["metadata"], {
            "name": "k",
            "namespace": "kube-system",
            "labels": {"sealedsecrets.bitnami.com/sealed-secrets-key": "active"},
        })
        self.assertEqual(manifest["data"], {"tls.crt": "Y3J0", "tls.key": "a2V5"})
        self.assertEqual(manifest["type"], "kubernetes.io/tls")


class ReadbackMismatches(unittest.TestCase):
    def result(self, written, read):
        return {"item": {"item": {"entry": "restic-password", "content": written, "description": "d"}}, "json": {"data": b64(read)}}

    def test_reports_only_versions_read_back_differently(self):
        results = [self.result("same", "same"), self.result("sent", "other"), {"skipped": True}]
        self.assertEqual(vault.vault_readback_mismatches(results), ["restic-password: d"])


if __name__ == "__main__":
    unittest.main()
