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


ARCHIVE_PREFIX = "MAIR-617 archived "


def vault_generated_writes(vault_values, finals, candidates, entries, label):
    """Versions to add for the generated secrets, in order.

    vault_values  {key: current version in the vault, '' when none};
    finals        {key: value that must be the current version}: the values
                  sealed on the instance, or the plaintext known for an admin
                  hash (resolve_generated.yml); a key is absent when nothing
                  has to be written;
    candidates    [{key, content, source}]: legacy files and leftovers;
    entries       {key: secret name in the vault}.

    Every candidate whose content is neither the current value nor the final
    one is stored first as an archive (`archive: True`, description
    ARCHIVE_PREFIX + its origin), so that no copy is lost before the files are
    deleted. vault_write.yml disables the archives, and vault_current_revisions
    never picks one: an archive never becomes the current value, even when the
    run stops before the final value is written. The final value comes last,
    only when it differs from the vault.
    """
    writes = []
    keys = sorted(set(vault_values) | set(finals) | {c["key"] for c in candidates})
    for key in keys:
        current = vault_values.get(key, "")
        final = finals.get(key)
        seen = {final, current, ""}
        for candidate in candidates:
            if candidate["key"] != key:
                continue
            content = candidate["content"].strip()
            if content in seen:
                continue
            seen.add(content)
            writes.append({
                "entry": entries[key],
                "content": content,
                "description": ARCHIVE_PREFIX + candidate["source"],
                "archive": True,
            })
        if final is not None and final != current:
            writes.append({"entry": entries[key], "content": final, "description": f"MAIR-617 {label}", "archive": False})
    return writes


def vault_current_revisions(listed, keys):
    """{key: revision of the current version, or None} from the version lists.

    `listed` are the results of the list loop (item = key, json.versions =
    [{revision, status, description}]). The current version is the newest
    enabled one that is not an archive: an archive written by a run that
    stopped before disabling it is skipped too.
    """
    by_key = {result.get("item"): result for result in listed or []}
    current = {}
    for key in keys:
        versions = (by_key.get(key, {}).get("json") or {}).get("versions") or []
        usable = [
            v["revision"] for v in versions
            if v.get("status", "enabled") == "enabled"
            and not (v.get("description") or "").startswith(ARCHIVE_PREFIX)
        ]
        current[key] = max(usable) if usable else None
    return current


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


def vault_quota_overflows(listed, writes, limit):
    """Secrets that `writes` would push past `limit` versions, as messages.

    Scaleway Secret Manager keeps at most 20 versions per secret, disabled
    ones included, and refuses the next one. Checked before writing anything,
    so that a run never stores half of what it planned. `listed` are the
    results of a loop over the written entries (item = secret name) of
    GET /secrets/<id>/versions, whose `total_count` counts every version.
    """
    stored = {result.get("item"): int((result.get("json") or {}).get("total_count", 0)) for result in listed or []}
    planned = {}
    for write in writes or []:
        planned[write["entry"]] = planned.get(write["entry"], 0) + 1
    return [
        f"{entry}: {stored.get(entry, 0)} versions stored + {count} new > {limit}"
        for entry, count in sorted(planned.items())
        if stored.get(entry, 0) + count > int(limit)
    ]


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
            "vault_current_revisions": vault_current_revisions,
            "vault_sealing_key_writes": vault_sealing_key_writes,
            "vault_quota_overflows": vault_quota_overflows,
            "vault_readback_mismatches": vault_readback_mismatches,
        }
