#!/usr/bin/env python3
"""Static check: image_updater_services matches the services Deploiment deploys.

    python3 tests/check_image_updater_services.py <Deploiment checkout>

`image_updater_services` (roles/k8s_argocd/defaults/main.yml) lists what
argocd-image-updater follows, by values path. The chart itself declares no
service (APIs|BFFs|Fronts.instances are empty in
charts/mairie360-stack/values.yaml): the instance keys live in
clusters/<org>/instances/<env>/values.yaml. Fails (exit 1) when:
  - an APIs|BFFs|Fronts instance with an image in any instance values file is
    not followed, or is followed under another image or alias;
  - a followed APIs|BFFs|Fronts path is in no instance values file (renamed or
    removed service: the ImageUpdater would write to a dead key);
  - a followed top-level path (database, liquibase) is not a key of the chart
    values.
"""

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
GROUPS = ("APIs", "BFFs", "Fronts")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    deploiment = pathlib.Path(sys.argv[1])
    defaults = yaml.safe_load((ROOT / "roles/k8s_argocd/defaults/main.yml").read_text())
    followed = {svc["path"]: svc for svc in defaults["image_updater_services"]}
    chart = yaml.safe_load((deploiment / "charts/mairie360-stack/values.yaml").read_text())

    deployed = {}  # values path -> {image: [files]}
    files = sorted(deploiment.glob("clusters/*/instances/*/values.yaml"))
    if not files:
        print(f"FAIL no clusters/*/instances/*/values.yaml under {deploiment}")
        return 1
    for path in files:
        values = yaml.safe_load(path.read_text()) or {}
        for group in GROUPS:
            for key, inst in ((values.get(group) or {}).get("instances") or {}).items():
                image = ((inst or {}).get("image") or {}).get("repository")
                if image:
                    deployed.setdefault(f"{group}.instances.{key}", {}).setdefault(image, []).append(
                        str(path.relative_to(deploiment)))

    failures = []
    for vpath, images in sorted(deployed.items()):
        svc = followed.get(vpath)
        if svc is None:
            failures.append(f"{vpath} ({', '.join(images)}) is deployed but not in image_updater_services")
            continue
        for image, where in images.items():
            if image != svc["image"]:
                failures.append(f"{vpath}: image_updater_services follows {svc['image']}, {', '.join(where)} deploys {image}")
        if svc["alias"] != vpath.rsplit(".", 1)[-1]:
            failures.append(f"{vpath}: alias {svc['alias']} does not follow the instance key")
    for vpath, svc in sorted(followed.items()):
        if vpath.split(".", 1)[0] in GROUPS:
            if vpath not in deployed:
                failures.append(f"{vpath} ({svc['image']}) is in image_updater_services but in no instance values file")
        elif vpath not in chart:
            failures.append(f"{vpath} ({svc['image']}) is in image_updater_services but not a key of the chart values")

    for f in failures:
        print(f"FAIL {f}")
    print(f"{len(followed)} followed services, {len(deployed)} deployed in {len(files)} values files, "
          f"{len(failures)} problem(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
