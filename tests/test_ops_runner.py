"""Operations from the CI (MAIR-803): python3 -m unittest tests/test_ops_runner.py -v

- the Operations workflow only runs from `main`, after approval, on the group's machine, and
  never runs the job's checkout there;
- the wrapper (roles/ops_runner/files/mairie360-ops) refuses anything but its operations,
  always runs origin/main, and hands the sealed secrets.yaml over (git and local repositories,
  ansible-playbook stubbed);
- scripts/ops-secrets-pr.sh opens a pull request only when secrets.yaml changed (gh stubbed);
- the CI inventories hold no public address and match role ops_runner.
"""

import ipaddress
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
WRAPPER = REPO / "roles/ops_runner/files/mairie360-ops"
PR_SCRIPT = REPO / "scripts/ops-secrets-pr.sh"
ROLE_DEFAULTS = yaml.safe_load((REPO / "roles/ops_runner/defaults/main.yml").read_text())
GROUP_VARS = yaml.safe_load((REPO / "inventory/group_vars/all.yml").read_text())

SEALED = """apiVersion: v1
items:
  - apiVersion: bitnami.com/v1alpha1
    kind: SealedSecret
    spec: {encryptedData: {X: AgB%s}}
"""


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def make_repo(root, name, files):
    """A bare repository `name.git` whose main holds `files`."""
    work = root / f"{name}-src"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    for path, content in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(content)
    git("add", "-A", cwd=work)
    git("commit", "-qm", "init", cwd=work)
    bare = root / f"{name}.git"
    git("clone", "-q", "--bare", str(work), str(bare))
    return work, bare


def load_workflow(name):
    data = yaml.safe_load((REPO / ".github/workflows" / name).read_text())
    data["on"] = data.pop(True, data.get("on"))
    return data


class Wrapper(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.ansible_src, ansible_bare = make_repo(self.root, "ansible", {
            "inventory/ci-mairie360.yml": "all: {}\n",
            "playbooks/secrets.yml": "[]\n",
            "ansible.cfg": "[defaults]\n",
        })
        _, deploiment_bare = make_repo(self.root, "deploiment", {
            "clusters/mairie360/instances/dev/values.yaml": "x: 1\n",
        })
        # ansible-playbook stub: logs its arguments and environment, and on secrets.yml writes
        # the secrets.yaml the role would fetch into deploiment_local_path.
        venv = self.root / "venv/bin"
        venv.mkdir(parents=True)
        self.log = self.root / "calls.log"
        stub = venv / "ansible-playbook"
        stub.write_text(f"""#!/usr/bin/env bash
{{ echo "ARGS $*"; echo "CWD $PWD"; echo "SCW_CONFIG_PATH=$SCW_CONFIG_PATH";
   echo "SCW_PROFILE=${{SCW_PROFILE-unset}}"; echo "SCW_SECRET_KEY=${{SCW_SECRET_KEY-}}";
   echo "ANSIBLE_CONFIG=$ANSIBLE_CONFIG"; echo "HEAD $(git rev-parse HEAD)"; }} >> {self.log}
for a in "$@"; do
  case "$a" in deploiment_local_path=*) d=${{a#deploiment_local_path=}} ;; esac
done
[ -z "${{d:-}}" ] || printf '%s' "$SEALED" > "$d/clusters/mairie360/instances/dev/secrets.yaml"
""")
        stub.chmod(0o755)
        self.config = self.root / "ops.env"
        group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
        user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
        self.work = self.root / "work"
        self.out = self.root / "out"
        self.config.write_text(f"""OPS_ORG=mairie360
OPS_ENVS="dev staging prod"
OPS_ANSIBLE_REPO={ansible_bare}
OPS_DEPLOIMENT_REPO={deploiment_bare}
OPS_WORK_DIR={self.work}
OPS_OUT_DIR={self.out}
OPS_OUT_GROUP={group}
OPS_OUT_OWNER={user}
OPS_VENV={self.root / "venv"}
OPS_COLLECTIONS={self.root / "collections"}
""")
        self.bare = ansible_bare

    def run_wrapper(self, *args, env=None):
        return subprocess.run(
            ["bash", str(WRAPPER), *args], capture_output=True, text=True,
            env={**os.environ, "MAIRIE360_OPS_CONFIG": str(self.config), "SCW_PROFILE": "operator",
                 "SEALED": SEALED % "1", **(env or {})},
        )

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_refuses_other_actions_groups_and_environments(self):
        for args in (["deploy", "mairie360", "dev"], ["secrets", "client-x", "dev"],
                     ["secrets", "mairie360", "preprod"], ["verify", "mairie360", "dev staging"],
                     ["secrets", "mairie360"], ["site", "mairie360", "dev"]):
            result = self.run_wrapper(*args)
            self.assertEqual(result.returncode, 2, args)
        self.assertFalse(self.work.exists(), "nothing cloned before the arguments are checked")
        self.assertEqual(self.calls(), "")

    def test_secrets_seals_from_main_and_hands_secrets_yaml_over(self):
        result = self.run_wrapper("secrets", "mairie360", "dev", env={"SCW_SECRET_KEY": "k"})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("playbooks/secrets.yml --limit mairie360-dev", calls)
        self.assertIn(f"-i {self.work}/ansible/inventory/ci-mairie360.yml", calls)
        self.assertIn("-e secrets_prompt=false", calls)
        self.assertIn(f"-e deploiment_local_path={self.work}/deploiment", calls)
        self.assertIn(f"-e legacy_backup_dir={self.work}/no-legacy-backup", calls)
        self.assertIn(f"CWD {self.work}/ansible", calls)
        self.assertIn(f"ANSIBLE_CONFIG={self.work}/ansible/ansible.cfg", calls)
        # Only the key passed by the job, never a CLI profile.
        self.assertIn("SCW_PROFILE=unset", calls)
        self.assertIn(f"SCW_CONFIG_PATH={self.work}/no-scw-config.yaml", calls)
        self.assertFalse((self.work / "no-scw-config.yaml").exists())
        self.assertIn("SCW_SECRET_KEY=k", calls)
        out = self.out / "mairie360-dev/secrets.yaml"
        self.assertEqual(out.read_text(), SEALED % "1")
        self.assertEqual(out.stat().st_mode & 0o777, 0o640)
        self.assertIn("mairie360/ansible at ", result.stdout)

    def test_runs_origin_main_whatever_the_clone_holds(self):
        self.assertEqual(self.run_wrapper("verify", "mairie360", "dev").returncode, 0)
        # A file left in the clone and a local edit are gone at the next run...
        clone = self.work / "ansible"
        (clone / "playbooks/verify.yml").write_text("- hosts: all\n")
        (clone / "ansible.cfg").write_text("[defaults]\nvault_password_file = /tmp/x\n")
        # ...and a new commit on main is picked up.
        (self.ansible_src / "NEW").write_text("1")
        git("add", "NEW", cwd=self.ansible_src)
        git("commit", "-qm", "new", cwd=self.ansible_src)
        git("push", "-q", str(self.bare), "main", cwd=self.ansible_src)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.ansible_src,
                              capture_output=True, text=True, check=True).stdout.strip()
        result = self.run_wrapper("verify", "mairie360", "staging")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((clone / "playbooks/verify.yml").exists())
        self.assertEqual((clone / "ansible.cfg").read_text(), "[defaults]\n")
        self.assertIn(f"HEAD {head}", self.calls().splitlines()[-1])
        self.assertIn(f"mairie360/ansible at {head}", result.stdout)

    def test_verify_targets_the_argocd_machine_and_the_instance(self):
        result = self.run_wrapper("verify", "mairie360", "prod")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("playbooks/verify.yml --limit mairie360_argocd:mairie360-prod", self.calls())
        self.assertFalse((self.work / "deploiment").exists())

    def test_fails_when_main_has_no_ci_inventory(self):
        (self.config).write_text(self.config.read_text().replace("OPS_ORG=mairie360", "OPS_ORG=other"))
        result = self.run_wrapper("verify", "other", "dev")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no CI inventory", result.stderr)
        self.assertEqual(self.calls(), "")


class SecretsPullRequest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        _, self.bare = make_repo(self.root, "deploiment", {
            "clusters/mairie360/instances/dev/secrets.yaml": SEALED % "old",
        })
        self.checkout = self.root / "checkout"
        git("clone", "-q", str(self.bare), str(self.checkout))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.gh_log = self.root / "gh.log"
        (bin_dir / "gh").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {self.gh_log}\n')
        (bin_dir / "gh").chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"

    def run_script(self, content, env_name="dev", org="mairie360"):
        sealed = self.root / "secrets.yaml"
        sealed.write_text(content)
        return subprocess.run(
            ["bash", str(PR_SCRIPT), str(self.checkout), str(sealed)], capture_output=True, text=True,
            env={**os.environ, "PATH": self.path, "ORG": org, "ENV_NAME": env_name, "GH_TOKEN": "t",
                 "GITHUB_RUN_ID": "42", "PUSH_URL": str(self.bare), "ACTOR": "someone",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    def test_unchanged_secrets_open_no_pull_request(self):
        result = self.run_script(SEALED % "old")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unchanged", result.stdout)
        self.assertFalse(self.gh_log.exists())

    def test_new_secrets_are_pushed_and_proposed(self):
        result = self.run_script(SEALED % "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        branch = "ops/seal-secrets-mairie360-dev-42"
        shown = subprocess.run(["git", "show", f"{branch}:clusters/mairie360/instances/dev/secrets.yaml"],
                               cwd=self.bare, capture_output=True, text=True, check=True).stdout
        self.assertEqual(shown, SEALED % "new")
        files = subprocess.run(["git", "show", "--name-only", "--format=%s", branch], cwd=self.bare,
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(files[:2], ["chore(dev):", "seal"])
        self.assertEqual(files[-1], "clusters/mairie360/instances/dev/secrets.yaml")
        args = self.gh_log.read_text().splitlines()
        self.assertEqual(args[:3], ["pr", "create", "--repo"])
        for flag, value in (("--base", "main"), ("--head", branch), ("--title", "chore(dev): seal secrets")):
            self.assertEqual(args[args.index(flag) + 1], value)

    def test_refuses_a_file_that_is_not_sealed_and_bad_names(self):
        self.assertEqual(self.run_script("password: hunter2\n").returncode, 1)
        self.assertEqual(self.run_script(SEALED % "x", env_name="../prod").returncode, 2)
        self.assertEqual(self.run_script(SEALED % "x", env_name="staging").returncode, 1)
        self.assertFalse(self.gh_log.exists())


class OperationsWorkflow(unittest.TestCase):
    def setUp(self):
        self.wf = load_workflow("ops.yml")
        self.run_job = self.wf["jobs"]["run"]
        self.pr_job = self.wf["jobs"]["pull-request"]

    def test_manual_main_only_and_never_cancelled(self):
        self.assertEqual(list(self.wf["on"]), ["workflow_dispatch"], "never on push or pull_request")
        self.assertEqual(self.run_job["if"], "github.ref == 'refs/heads/main'")
        self.assertIs(self.wf["concurrency"]["cancel-in-progress"], False)
        self.assertEqual(self.wf["permissions"], {"contents": "read"})

    def test_runs_after_approval_on_the_group_machine(self):
        self.assertEqual(self.run_job["environment"], "ops-${{ inputs.org }}-${{ inputs.env }}")
        self.assertIn("mairie360-ops-{0}", self.run_job["runs-on"])
        self.assertIn('"self-hosted"', self.run_job["runs-on"])

    def test_the_machine_job_runs_only_the_wrapper(self):
        steps = self.run_job["steps"]
        self.assertFalse(any("checkout" in str(s.get("uses", "")) for s in steps),
                         "no checkout: the job's code never runs on the machine")
        runs = [s["run"] for s in steps if "run" in s]
        self.assertEqual(runs, ['sudo -n /usr/local/sbin/mairie360-ops "$ACTION" "$ORG" "$ENV_NAME"'])
        # Inputs reach the shell through the environment, never interpolated in the script.
        for step in steps:
            self.assertNotIn("${{", step.get("run", ""))

    def test_inputs_match_the_wrapper_and_the_ci_inventories(self):
        inputs = self.wf["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["action"]["options"], ["secrets", "verify"])
        for org in inputs["org"]["options"]:
            inventory = yaml.safe_load((REPO / f"inventory/ci-{org}.yml").read_text())
            envs = {h["env_name"] for h in inventory["all"]["children"][f"{org.replace('-', '_')}_instances"]["hosts"].values()}
            self.assertLessEqual(set(inputs["env"]["options"]), envs, org)

    def test_release_app_key_stays_off_the_machine(self):
        self.assertEqual(self.pr_job["runs-on"], "ubuntu-latest")
        self.assertEqual(self.pr_job["needs"], "run")
        self.assertNotIn("RELEASE_APP", yaml.safe_dump(self.run_job))
        self.assertNotIn("SCW_", yaml.safe_dump(self.pr_job))
        self.assertIn("inputs.action == 'secrets'", self.pr_job["if"])

    def test_actions_pinned_and_checkouts_without_token(self):
        for name, job in self.wf["jobs"].items():
            for step in job["steps"]:
                uses = str(step.get("uses", ""))
                if uses:
                    self.assertRegex(uses, r"@[0-9a-f]{40}$", name)
                if uses.startswith("actions/checkout"):
                    self.assertIs(step["with"].get("persist-credentials"), False, name)


class CiInventory(unittest.TestCase):
    def test_ci_inventories_match_role_ops_runner(self):
        wg_subnet = ipaddress.ip_network(GROUP_VARS["wg_subnet"])
        paths = sorted((REPO / "inventory").glob("ci-*.yml"))
        self.assertTrue(paths)
        for path in paths:
            org = path.stem[len("ci-"):]
            group = org.replace("-", "_")
            data = yaml.safe_load(path.read_text())["all"]
            self.assertEqual(data["vars"]["ansible_user"], ROLE_DEFAULTS["ops_runner_instance_user"])
            self.assertEqual(data["vars"]["ansible_ssh_private_key_file"], ROLE_DEFAULTS["ops_runner_ssh_key"])
            argocd = data["children"][f"{group}_argocd"]["hosts"]
            self.assertEqual(len(argocd), 1)
            self.assertEqual(next(iter(argocd.values()))["ansible_connection"], "local")
            instances = data["children"][f"{group}_instances"]["hosts"]
            for name, host in instances.items():
                self.assertEqual(name, f"{org}-{host['env_name']}", "the wrapper limits to <org>-<env>")
                self.assertEqual(host["ansible_host"], host["wg_ip"], "over WireGuard only")
                self.assertIn(ipaddress.ip_address(host["wg_ip"]), wg_subnet)
                self.assertIn("instance_domain", host)
            self.assertEqual(len({h["env_name"] for h in instances.values()}), len(instances))


if __name__ == "__main__":
    unittest.main()
