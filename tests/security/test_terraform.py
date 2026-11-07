"""The node and its firewall, planned and evaluated rather than eyeballed.

docs/SECURITY.md says that in tunnel mode one inbound rule exists, port 22 from
admin_cidrs, that a domain adds 80 and 443 and nothing else, that GPU_COUNT
picks the plan, and that everything carries the tag destroy.sh looks for.

deploy/terraform/tests/node.tftest.hcl plans the config against a mock of the
Linode provider and asserts each of those. This runs it with LINODE_TOKEN taken
out of the environment, and checks the state file before and after. It also
asks `terraform console` for what the cloud-init file renders to, which a plan
cannot show in a useful form. Neither makes an API call or touches state. Both
need `terraform init`, which downloads the provider the lock file pins.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
import yaml

from tests.conftest import REPO_ROOT, load_script

TF_DIR = REPO_ROOT / "deploy" / "terraform"
TFTEST = TF_DIR / "tests" / "node.tftest.hcl"
ADMIN_CIDR = "203.0.113.4/32"
# Any well-formed public key. It reaches nothing.
SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleEx test"


def without_token() -> dict[str, str]:
    """This box's environment less the token, so nothing here could log in."""
    return {name: value for name, value in os.environ.items() if name != "LINODE_TOKEN"}


@cache
def terraform() -> str:
    """Skip, with the reason, when terraform cannot run these tests here."""
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    found = subprocess.run(["terraform", "version", "-json"], capture_output=True, text=True)
    version = tuple(int(n) for n in json.loads(found.stdout)["terraform_version"].split(".")[:2])
    if version < (1, 7):
        pytest.skip("terraform test with a mock provider needs terraform 1.7 or newer")
    if not (TF_DIR / ".terraform").is_dir():
        init = subprocess.run(
            [
                "terraform",
                f"-chdir={TF_DIR}",
                "init",
                "-input=false",
                "-backend=false",
                "-no-color",
            ],
            capture_output=True,
            text=True,
            env=without_token(),
            timeout=300,
        )
        if init.returncode != 0:
            pytest.skip(f"terraform init could not fetch the provider: {init.stderr[-300:]}")
    return "terraform"


def console(expression: str, domain: str = "", gpu_count: int = 1) -> object:
    """One expression against the real config, as Python.

    jsonencode on the way out, because `terraform console` prints HCL. That comes
    back as a JSON string, so it is decoded twice.
    """
    result = subprocess.run(
        [
            terraform(),
            f"-chdir={TF_DIR}",
            "console",
            "-no-color",
            "-var",
            f"ssh_public_key={SSH_PUBLIC_KEY}",
            "-var",
            f'admin_cidrs=["{ADMIN_CIDR}"]',
            "-var",
            f"domain={domain}",
            "-var",
            f"gpu_count={gpu_count}",
        ],
        input=f"jsonencode({expression})\n",
        capture_output=True,
        text=True,
        env=without_token(),
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"terraform console failed:\n{result.stderr}")
    # The value is the last line. A deploy.auto.tfvars left in the directory by
    # make deploy is loaded too, and console prints a warning on stdout for any
    # line in it the config no longer declares, ahead of the value.
    return json.loads(json.loads(result.stdout.strip().splitlines()[-1]))


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


# ---- the plans --------------------------------------------------------------


def test_the_plans_against_the_mock_provider_pass_and_touch_no_state() -> None:
    state = TF_DIR / "terraform.tfstate"
    before = digest(state)
    result = subprocess.run(
        [terraform(), f"-chdir={TF_DIR}", "test", "-no-color"],
        capture_output=True,
        text=True,
        env=without_token(),
        timeout=300,
    )
    runs = len(re.findall(r'^run "', TFTEST.read_text(), re.M))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Success! {runs} passed, 0 failed." in result.stdout
    assert digest(state) == before


def test_every_run_is_a_plan_on_a_mocked_provider() -> None:
    """An apply here, or a provider left unmocked, would reach the real API."""
    source = TFTEST.read_text()
    runs = re.findall(r'^run "', source, re.M)
    plans = re.findall(r"^\s+command = plan$", source, re.M)
    assert runs and len(plans) == len(runs)
    assert "apply" not in re.sub(r"#.*", "", source)
    versions = (TF_DIR / "versions.tf").read_text()
    providers = set(re.findall(r"^\s+(\w+) = \{\n\s+source", versions, re.M))
    mocked = set(re.findall(r'^mock_provider "(\w+)"', source, re.M))
    assert providers == mocked == {"linode"}


# ---- the firewall -------------------------------------------------------------


def test_tunnel_mode_opens_one_port_to_one_address() -> None:
    """The default. Nothing but ssh, and only from the address that deployed it."""
    assert console("local.inbound_rules") == [
        {"label": "ssh-admin", "protocol": "TCP", "ports": "22", "ipv4": [ADMIN_CIDR]}
    ]


def test_domain_mode_adds_only_80_and_443() -> None:
    """TLS needs both: 80 for the ACME challenge and the redirect, 443 for traffic."""
    rules = console("local.inbound_rules", domain="cards.example.com")
    by_port = {rule["ports"]: rule for rule in rules}
    assert list(by_port) == ["22", "80", "443"]
    assert by_port["22"]["ipv4"] == [ADMIN_CIDR], "ssh must stay locked to admin_cidrs"
    for port in ("80", "443"):
        assert by_port[port]["ipv4"] == ["0.0.0.0/0"]
        assert by_port[port]["protocol"] == "TCP"


def test_the_firewall_drops_by_default_and_attaches_to_the_node() -> None:
    """A rule list is only an allow list if the policy under it is DROP."""
    source = (TF_DIR / "main.tf").read_text()
    assert 'inbound_policy  = "DROP"' in source
    assert "linodes = [linode_instance.node.id]" in source
    # Interfaces on the configuration profile, so the firewall binds to the instance.
    assert 'interface_generation = "legacy_config"' in source


# ---- the plan and the tag -------------------------------------------------------


def test_gpu_count_picks_the_plan_gpu_check_checks() -> None:
    """deploy/scripts/gpu_check.py asks the API about the plan before terraform
    asks for it. The two have to be talking about the same one."""
    gpu_check = load_script(REPO_ROOT / "deploy" / "scripts" / "gpu_check.py")
    plans = console("local.plans")
    assert {int(count): plan for count, plan in plans.items()} == gpu_check.PLANS
    for count, plan in gpu_check.PLANS.items():
        assert console("local.instance_type", gpu_count=count) == plan


def test_every_resource_carries_the_tag_the_teardown_looks_for() -> None:
    """destroy.sh asks the API for this tag. A resource without it is invisible
    to that check, and a resource in the state is all destroy deletes."""
    tag = console("local.tag")
    assert tag == "baseball-card"
    lib = (REPO_ROOT / "deploy" / "scripts" / "lib.sh").read_text()
    assert re.search(r"^TAG=(\S+)$", lib, re.M).group(1) == tag

    source = (TF_DIR / "main.tf").read_text()
    resources = re.findall(r'^resource "(\w+)" "(\w+)"', source, re.M)
    assert resources == [("linode_instance", "node"), ("linode_firewall", "node")]
    blocks = re.split(r'^resource "', source, flags=re.M)[1:]
    for block in blocks:
        assert re.search(r"^  tags\s+= \[local\.tag\]$", block, re.M), block.splitlines()[0]


# ---- cloud-init ---------------------------------------------------------------


@cache
def user_data() -> dict:
    rendered = console("local.user_data")
    assert isinstance(rendered, str) and rendered.startswith("#cloud-config")
    return yaml.safe_load(rendered)


def test_the_cloud_init_renders_and_is_yaml() -> None:
    """The user-data is a Terraform template. A stray ${...} in it breaks the deploy."""
    doc = user_data()
    assert doc["disable_root"] is True
    assert doc["ssh_pwauth"] is False
    assert [user["name"] for user in doc["users"]] == ["deploy"]
    assert doc["users"][0]["lock_passwd"] is True
    assert doc["users"][0]["ssh_authorized_keys"] == [SSH_PUBLIC_KEY]
    assert "docker" in doc["users"][0]["groups"]
    assert doc["power_state"]["mode"] == "reboot"


def test_the_bootstrap_script_survives_the_round_trip() -> None:
    """It travels base64 encoded. If that ever breaks, the node gets half a script."""
    written = [f for f in user_data()["write_files"] if f["path"].endswith("bootstrap.sh")]
    assert len(written) == 1
    assert written[0]["encoding"] == "b64"
    decoded = base64.b64decode(written[0]["content"]).decode()
    assert decoded == (REPO_ROOT / "deploy" / "cloud-init" / "bootstrap.sh").read_text()


def test_sshd_is_hardened_on_the_node() -> None:
    """Key only, no root, no passwords. The drop-in is the only place this is set."""
    dropin = [f for f in user_data()["write_files"] if "sshd_config.d" in f["path"]]
    assert len(dropin) == 1
    config = dropin[0]["content"].splitlines()
    for line in (
        "PermitRootLogin no",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitEmptyPasswords no",
        "AuthenticationMethods publickey",
        "AllowAgentForwarding no",
    ):
        assert line in config, f"the sshd drop-in is missing: {line}"


def test_the_scripts_wait_for_the_marker_cloud_init_writes() -> None:
    """bootstrap.sh writes "installed", bootcmd turns it into "ready" on the next
    boot, and deploy.sh waits for "ready". Three files, one path."""
    bootstrap = (REPO_ROOT / "deploy" / "cloud-init" / "bootstrap.sh").read_text()
    state_dir = re.search(r"^STATE_DIR=(\S+)$", bootstrap, re.M).group(1)
    assert 'date -Is >"$STATE_DIR/installed"' in bootstrap
    bootcmd = " ".join(user_data()["bootcmd"][0])
    assert f"test -f {state_dir}/installed" in bootcmd
    assert f">{state_dir}/ready" in bootcmd
    lib = (REPO_ROOT / "deploy" / "scripts" / "lib.sh").read_text()
    assert f"READY_MARKER={state_dir}/ready" in lib
    log = re.search(r"^LOG=(\S+)$", bootstrap, re.M).group(1)
    assert f"BOOTSTRAP_LOG={log}" in lib


def test_the_repo_lands_where_bootstrap_made_room_for_it() -> None:
    bootstrap = (REPO_ROOT / "deploy" / "cloud-init" / "bootstrap.sh").read_text()
    lib = (REPO_ROOT / "deploy" / "scripts" / "lib.sh").read_text()
    node_dir = re.search(r"^NODE_DIR=(\S+)$", lib, re.M).group(1)
    assert f"install -d -m 0755 {node_dir}" in bootstrap
    assert f'chown "$DEPLOY_USER:$(id -gn "$DEPLOY_USER")" {node_dir}' in bootstrap
