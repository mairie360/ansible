"""Filters of role k8s_instance_secrets for the Secret Manager vault (MAIR-617).

Pure functions: they only decide what to write, the role's tasks do the HTTP
calls. Unit-tested by tests/test_vault_filters.py (no network, no cluster).
"""

import base64
import json
import os

import yaml


def _b64decode(value):
    return base64.b64decode(value).decode("utf-8")


def vault_ids(listed, path, names):
    """{name: id} of the secrets called one of `names` stored exactly under `path`.

    `listed` is the `secrets` list of GET /secrets?project_id=&path=.
    """
    names = set(names)
    return {
        secret["name"]: secret["id"]
        for secret in listed or []
        if secret.get("path") == path and secret.get("name") in names
    }


def vault_missing(ids, names):
    """Names of `names` that have no id in `ids`, sorted."""
    return sorted(name for name in names if name not in ids)


def vault_legacy_files(slurped, prefixes, org, env, origin):
    """Sort the local or leftover copies of the generated secrets of MAIR-599.

    `slurped` are the results of a slurp loop (`source`, base64 `content`);
    `prefixes` maps each generated secret to its file prefix
    ({restic_password: restic, admin_password: admin}). A file belongs to the
    instance when its name starts with `<prefix>-<org>-<env>.txt`: the plain
    name is the current copy, suffixed ones (`.from-<host>`, `.<pid>.<date>~`,
    a date) are older copies.

    Returns {"exact": {key: content of the plain file},
             "candidates": [{key, content, source}] for every file}.
    """
    exact = {}
    candidates = []
    for result in slurped or []:
        if result.get("skipped") or "content" not in result:
            continue
        source = result["source"]
        name = os.path.basename(source)
        for key, prefix in prefixes.items():
            plain = f"{prefix}-{org}-{env}.txt"
            if not name.startswith(plain):
                continue
            content = _b64decode(result["content"]).strip()
            if name == plain:
                exact[key] = content
            candidates.append({"key": key, "content": content, "source": f"{origin}:{source}"})
            break
    return {"exact": exact, "candidates": candidates}


def vault_generated_writes(vault_values, finals, candidates, entries, label):
    """Versions to add for the generated secrets, in order.

    vault_values  {key: latest enabled version in the vault, '' when none};
    finals        {key: value that must be the latest version}: the values
                  sealed on the instance (generated_backup_writes); a key is
                  absent when nothing has to be written (admin hash on the
                  instance);
    candidates    [{key, content, source}]: legacy files and leftovers;
    entries       {key: secret name in the vault}.

    Every candidate whose content is not already the latest (or about to be)
    is archived as its own version first, so that no copy is lost before the
    files are deleted. Then the final value is written when it differs from
    the vault, or written again after archives so that it stays the latest.
    When there is no final value but archives were added, the previous latest
    is written again for the same reason.
    """
    writes = []
    keys = sorted(set(vault_values) | set(finals) | {c["key"] for c in candidates})
    for key in keys:
        current = vault_values.get(key, "")
        final = finals.get(key)
        latest = final if final is not None else current
        seen = {latest, current, ""}
        archives = []
        for candidate in candidates:
            if candidate["key"] != key:
                continue
            content = candidate["content"].strip()
            if content in seen:
                continue
            seen.add(content)
            archives.append({
                "entry": entries[key],
                "content": content,
                "description": f"MAIR-617 archived {candidate['source']}",
            })
        writes.extend(archives)
        if final is not None and (final != current or archives):
            writes.append({"entry": entries[key], "content": final, "description": f"MAIR-617 {label}"})
        elif final is None and archives and current:
            writes.append({"entry": entries[key], "content": current, "description": "MAIR-617 latest kept after archiving"})
    return writes


def sealing_key_manifest(secret):
    """The Secret manifest of one sealing key, without cluster-assigned fields.

    `kubectl apply -f` of it restores the key on a reinstalled instance.
    """
    # Plain types: Ansible hands over its own tagged dict/str subclasses,
    # which yaml.safe_dump refuses.
    secret = json.loads(json.dumps(secret))
    metadata = secret["metadata"]
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata.get("namespace", "kube-system"),
            "labels": metadata.get("labels", {}),
        },
        "type": secret.get("type", "kubernetes.io/tls"),
        "data": secret["data"],
    }
    return yaml.safe_dump(manifest, sort_keys=True)


def vault_sealing_key_writes(cluster_keys, legacy_keys, stored, entry):
    """Versions to add to the sealing-keys secret: one per key not stored yet.

    A version's description is the key's Secret name, so `stored` (the
    descriptions of the existing versions) tells which keys are already in
    the vault. Keys of the instance come first, then keys only found in the
    legacy local backup (a key the controller no longer has still decrypts
    old secrets.yaml revisions).
    """
    done = set(stored or [])
    writes = []
    for keys in (cluster_keys or [], legacy_keys or []):
        for secret in sorted(keys, key=lambda s: s["metadata"]["name"]):
            name = secret["metadata"]["name"]
            if name in done:
                continue
            done.add(name)
            writes.append({"entry": entry, "content": sealing_key_manifest(secret), "description": name})
    return writes


def vault_readback_mismatches(results):
    """Descriptions of the versions whose data read back differs from what was written.

    `results` are the results of the read-back loop: item.item is the write
    ({entry, content, description}), json.data the base64 data read back.
    """
    mismatches = []
    for result in results or []:
        if result.get("skipped"):
            continue
        written = result["item"]["item"]
        if _b64decode(result["json"]["data"]) != written["content"]:
            mismatches.append(f"{written['entry']}: {written['description']}")
    return mismatches


class FilterModule:
    def filters(self):
        return {
            "vault_ids": vault_ids,
            "vault_missing": vault_missing,
            "vault_legacy_files": vault_legacy_files,
            "vault_generated_writes": vault_generated_writes,
            "vault_sealing_key_writes": vault_sealing_key_writes,
            "vault_readback_mismatches": vault_readback_mismatches,
        }
