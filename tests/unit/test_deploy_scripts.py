"""The deploy scripts, run end to end with no cloud and no node.

Each test copies deploy/ into a throwaway repo and puts stand-ins first on PATH
for terraform, ssh, rsync, curl, openssl and uv. The stand-in terraform keeps
its state in a file the test writes. The stand-in ssh plays the node: it
answers the commands the scripts send it, keeps the node's .env, and for
make rotate-key holds which keys the node takes. curl plays the Linode API and
the IP echo. uv plays gpu_check.py and cardctl. ssh-keygen and jq are the real
ones. Every call is written down, so a test can check what ran, in what order,
and that no secret was ever an argument.

tests/unit/test_gpu_check.py tests gpu_check.py itself, against the API's
recorded answers, and test_deploy_node_env.py the node's .env.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"
KEY = "k" * 64
TOKEN = "made-up-linode-token-for-the-fakes"
IP = "192.0.2.10"
OTHER = "198.51.100.7"

# ---- the stand-ins ----------------------------------------------------------

FAKE_TERRAFORM = r"""#!/usr/bin/env bash
printf 'terraform %s\n' "$*" >> "$FAKE_LOG"
dir=""
case "$1" in -chdir=*) dir="${1#-chdir=}"; shift ;; esac
case "$1" in
init) exit 0 ;;
apply)
	cat "$FAKE_DIR/apply.out"
	cp "$FAKE_DIR/state.after" "$FAKE_DIR/state.json"
	if grep -q linode_instance "$FAKE_DIR/state.json"; then : >"$dir/terraform.tfstate"; fi
	exit "$(cat "$FAKE_DIR/apply.rc")"
	;;
destroy)
	printf '{"format_version": "1.0"}' >"$FAKE_DIR/state.json"
	rm -f "$dir/terraform.tfstate"
	echo "Destroy complete! Resources: 2 destroyed."
	;;
show) cat "$FAKE_DIR/state.json" ;;
state) jq -r '.values.root_module.resources[]?.address' "$FAKE_DIR/state.json" ;;
output)
	case "$3" in
	ip_address) jq -r '.values.root_module.resources[0].values.ipv4[0]' "$FAKE_DIR/state.json" ;;
	deploy_user) printf deploy ;;
	label) printf baseball-card ;;
	instance_type) printf '%s' "${FAKE_TYPE:-g2-gpu-rtx4000a1-m}" ;;
	region) printf us-ord ;;
	*) exit 1 ;;
	esac
	;;
esac
exit 0
"""

# The node. The remote command is the last argument. Keys the node takes are in
# $FAKE_DIR/active, one a line, and the key its .env holds in $FAKE_DIR/node_key.
FAKE_SSH = r"""#!/usr/bin/env bash
remote="${!#}"
printf 'ssh %s\n' "$remote" >> "$FAKE_LOG"
printf '%s\n' "$*" >> "$FAKE_DIR/ssh-args"
if [ "${FAKE_SSH_DOWN:-0}" = 1 ]; then
	if ssh-keygen -F "$FAKE_IP" -f "$FAKE_KNOWN_HOSTS" >/dev/null 2>&1; then
		echo 'ssh with the old key known' >> "$FAKE_LOG"
	else
		echo 'ssh with no key known' >> "$FAKE_LOG"
	fi
	exit 255
fi
case "$remote" in
*"curl --config -"*)
	key="$(sed -n 's/^header = "Authorization: Bearer \(.*\)"$/\1/p')"
	if grep -qxF -- "$key" "$FAKE_DIR/active" 2>/dev/null; then printf 200; else printf 401; fi
	;;
*"grep -v '^CARD_API_KEY='"*) sed -n 's/^CARD_API_KEY=//p' > "$FAKE_DIR/node_key" ;;
*"docker compose up -d")
	if [ "${FAKE_KEEP_OLD:-0}" = 1 ]; then
		cat "$FAKE_DIR/node_key" >> "$FAKE_DIR/active"
	else
		cp "$FAKE_DIR/node_key" "$FAKE_DIR/active"
	fi
	;;
true | "test -f "*) ;;
*"nvidia-smi -L 2>/dev/null | wc -l"*) echo "${FAKE_GPUS:-1}" ;;
"nvidia-smi -L") echo "GPU 0: NVIDIA RTX 4000 Ada Generation" ;;
*"cat > /opt/baseball-card/.env.tmp"*) cat > "$FAKE_DIR/node.env" ;;
*"docker compose exec -T api python -"*)
	cat >/dev/null
	printf '%s' '{"ok":true,"services":{"cutout":{"model":"ZhengPeng7/BiRefNet",'
	echo '"ran_on":"akamai-gpu0","vram_mb":2012}}}'
	;;
*"/livez"*) printf 200 ;;
esac
exit 0
"""

# The Linode API and the IP echo. A call with --config - has its config, the
# token's header line, read off stdin into curl-stdin, so a test can see the
# token went there and not into an argument.
FAKE_CURL = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$FAKE_LOG"
for arg in "$@"; do
	if [ "$arg" = "--config" ]; then cat >> "$FAKE_DIR/curl-stdin"; break; fi
done
url="${!#}"
case "$url" in
*/profile) echo '{"username": "someone"}' ;;
*whatismyip.akamai.com*) printf '%s' "$FAKE_MY_IP" ;;
*/linode/types/*) echo '{"price": {"hourly": 0.67}}' ;;
*/linode/instances*) cat "$FAKE_DIR/instances.json" ;;
*/networking/firewalls*) cat "$FAKE_DIR/firewalls.json" ;;
esac
exit 0
"""

FAKE_RSYNC = r"""#!/usr/bin/env bash
printf 'rsync %s\n' "$*" >> "$FAKE_LOG"
"""

FAKE_OPENSSL = r"""#!/usr/bin/env bash
printf 'openssl %s\n' "$*" >> "$FAKE_LOG"
printf 'r%.0s' $(seq 64)
echo
"""

# gpu_check.py exits with FAKE_GPU_CHECK. cardctl writes two PNG headers at the
# size the smoke test checks, and the ledger the test hands it.
FAKE_UV = r"""#!/usr/bin/env bash
printf 'uv %s\n' "$*" >> "$FAKE_LOG"
case "$*" in
*gpu_check.py*)
	echo "the gpu-check table"
	exit "${FAKE_GPU_CHECK:-0}"
	;;
*cardctl*)
	out=""
	while [ $# -gt 0 ]; do
		if [ "$1" = --out ]; then out="$2"; fi
		shift
	done
	mkdir -p "$out"
	cp "$FAKE_DIR/card.png" "$out/card-front.png"
	cp "$FAKE_DIR/card.png" "$out/card-back.png"
	cp "$FAKE_DIR/ledger.json" "$out/card.json"
	;;
esac
"""

FAKES = {
    "terraform": FAKE_TERRAFORM,
    "ssh": FAKE_SSH,
    "curl": FAKE_CURL,
    "rsync": FAKE_RSYNC,
    "openssl": FAKE_OPENSSL,
    "uv": FAKE_UV,
}

# What terraform apply prints in each case, trimmed to the lines that matter.
NO_CHANGES = "No changes. Your infrastructure matches the configuration.\n"
CREATED = "linode_instance.node: Creation complete after 1m2s [id=222]\nApply complete!\n"
REPLACED = "linode_instance.node: Destruction complete after 8s\nApply complete!\n"
FAILED_AFTER_CREATE = "linode_instance.node: Creation complete [id=222]\nError: invalid rule\n"


def state(instance: str | None) -> str:
    """terraform show -json for a state holding one instance, or none."""
    if instance is None:
        return json.dumps({"format_version": "1.0"})
    resources = [
        {"address": "linode_instance.node", "values": {"id": instance, "ipv4": [IP]}},
        {"address": "linode_firewall.node", "values": {"id": "9"}},
    ]
    return json.dumps(
        {"format_version": "1.0", "values": {"root_module": {"resources": resources}}}
    )


def png_header(width: int, height: int) -> bytes:
    """Enough of a PNG for smoke.sh, which reads the size out of the IHDR."""
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    return b"\x89PNG\r\n\x1a\n" + ihdr


class Box:
    """A throwaway copy of the repo's deploy side, with the stand-ins on PATH."""

    def __init__(self, tmp_path: Path, dotenv: str, before: str | None = None) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        for part in ("deploy/scripts", "deploy/layouts", "deploy/terraform"):
            (self.repo / part).mkdir(parents=True)
        for path in (DEPLOY / "scripts").iterdir():
            if path.is_file():
                shutil.copy2(path, self.repo / "deploy" / "scripts" / path.name)
        for path in (DEPLOY / "layouts").iterdir():
            shutil.copy2(path, self.repo / "deploy" / "layouts" / path.name)
        for name in ("deploy/models.env", ".gitignore", ".env.example"):
            shutil.copy2(REPO_ROOT / name, self.repo / name)
        (self.repo / ".env").write_text(dotenv)

        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        self.set_state(before)
        (self.fake / "state.after").write_text(state(before))
        (self.fake / "apply.out").write_text(NO_CHANGES)
        (self.fake / "apply.rc").write_text("0")
        self.answer("instances", [])
        self.answer("firewalls", [])
        (self.fake / "card.png").write_bytes(png_header(1500, 2100))
        self.known_hosts = self.repo / "secrets" / "ssh" / "known_hosts"

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = tmp_path / "calls.log"
        self.log.touch()

    def set_state(self, instance: str | None) -> None:
        (self.fake / "state.json").write_text(state(instance))
        tfstate = self.repo / "deploy" / "terraform" / "terraform.tfstate"
        if instance is None:
            tfstate.unlink(missing_ok=True)
        else:
            tfstate.write_text("{}")

    def answer(self, route: str, rows: list[dict]) -> None:
        """What the stand-in API lists for /linode/instances or /networking/firewalls."""
        body = {"data": rows, "page": 1, "pages": 1, "results": len(rows)}
        (self.fake / f"{route}.json").write_text(json.dumps(body))

    def run(self, script: str, *args: str, stdin: str | None = None, **env: str):
        result = subprocess.run(
            ["bash", str(self.repo / "deploy" / "scripts" / script), *args],
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.tmp),
                "LINODE_TOKEN": TOKEN,
                "FAKE_LOG": str(self.log),
                "FAKE_DIR": str(self.fake),
                "FAKE_IP": IP,
                # Inside DEPLOYABLE's ADMIN_CIDR, as a laptop deploying from its own address is.
                "FAKE_MY_IP": "203.0.113.4",
                "FAKE_KNOWN_HOSTS": str(self.known_hosts),
                "SSH_WAIT_S": "0",
                "BOOTSTRAP_WAIT_S": "0",
                "HEALTH_WAIT_S": "0",
                **env,
            },
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.calls = self.log.read_text().splitlines()
        return result

    def called(self, prefix: str) -> list[str]:
        return [call for call in self.calls if call.startswith(prefix)]

    def tfvars(self) -> str:
        return (self.repo / "deploy" / "terraform" / "deploy.auto.tfvars").read_text()


DEPLOYABLE = f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\nADMIN_CIDR=203.0.113.4/32\n"


def new_node(tmp_path: Path, dotenv: str = DEPLOYABLE) -> Box:
    """A box whose terraform apply makes instance 222 at IP."""
    box = Box(tmp_path, dotenv)
    (box.fake / "state.after").write_text(state("222"))
    (box.fake / "apply.out").write_text(CREATED)
    return box


def no_secret_in_any_argument(box: Box) -> None:
    """The key and the token travel on stdin. Neither is ever an argument."""
    args = (
        "\n".join(box.calls) + (box.fake / "ssh-args").read_text()
        if (box.fake / "ssh-args").exists()
        else "\n".join(box.calls)
    )
    if KEY in args:
        raise AssertionError("the key was an argument of a command")
    if TOKEN in args:
        raise AssertionError("the Linode token was an argument of a command")


# ---- make deploy: what stops it -------------------------------------------------


def test_deploy_refuses_without_the_flag_and_runs_nothing(tmp_path) -> None:
    box = Box(tmp_path, f"ALLOW_CLOUD_DEPLOY=0\nCARD_API_KEY={KEY}\n")
    done = box.run("deploy.sh")
    assert done.returncode != 0
    assert "ALLOW_CLOUD_DEPLOY is not 1 in .env" in done.stderr
    assert box.calls == []


def test_deploy_refuses_without_a_token(tmp_path) -> None:
    box = Box(tmp_path, DEPLOYABLE)
    done = box.run("deploy.sh", LINODE_TOKEN="")
    assert done.returncode != 0
    assert "LINODE_TOKEN is not set" in done.stderr
    assert box.calls == []


@pytest.mark.parametrize(
    ("status", "says"),
    [
        (3, "cannot sell the plan right now. The table above names the regions that can."),
        (1, "make gpu-check could not answer, so nothing was created."),
        (2, "the settings in .env pick no plan this repo can run on."),
    ],
)
def test_gpu_check_stops_the_deploy_before_anything_is_made(tmp_path, status, says) -> None:
    """The plan cannot be sold, or the API cannot say: learn it in seconds, not
    twenty minutes into terraform."""
    box = Box(tmp_path, DEPLOYABLE)
    done = box.run("deploy.sh", FAKE_GPU_CHECK=str(status))
    assert done.returncode != 0
    assert says in done.stderr
    assert "the gpu-check table" in done.stderr
    assert box.called("terraform") == []
    assert not (box.repo / "secrets").exists()
    assert not (box.repo / "deploy" / "terraform" / "deploy.auto.tfvars").exists()


def test_gpu_check_is_asked_about_the_settings_the_deploy_uses(tmp_path) -> None:
    box = Box(tmp_path, DEPLOYABLE + "GPU_COUNT=2\nLINODE_REGION=fr-par\n")
    box.run("deploy.sh", FAKE_GPU_CHECK="3")
    (call,) = box.called("uv")
    assert call.endswith("deploy/scripts/gpu_check.py")
    assert "fr-par cannot sell the plan" in box.run("deploy.sh", FAKE_GPU_CHECK="3").stderr


# ---- make deploy: a whole run -------------------------------------------------


def test_a_whole_deploy_against_a_stand_in_node(tmp_path) -> None:
    box = new_node(tmp_path)
    done = box.run("deploy.sh")
    assert done.returncode == 0, done.stderr

    steps = [
        "uv run --quiet",
        "terraform -chdir=",
        "ssh true",
        "ssh test -f /var/lib/baseball-card/ready",
        "rsync ",
        "ssh umask 077 && cat > /opt/baseball-card/.env.tmp",
        "ssh cd /opt/baseball-card && ./deploy/scripts/pull-models.sh",
        "ssh cd /opt/baseball-card && docker build -f services/vision-base.Dockerfile"
        " -t baseball-card-vision-base:local .",
        "ssh cd /opt/baseball-card && docker compose build",
        "ssh cd /opt/baseball-card && docker compose up -d",
        "ssh cd /opt/baseball-card && docker compose restart caddy",
        "ssh cd /opt/baseball-card && docker compose exec -T api python -",
    ]
    at = [next(i for i, call in enumerate(box.calls) if call.startswith(step)) for step in steps]
    assert at == sorted(at), "the stages ran out of order"
    assert "the node is up  192.0.2.10  tunnel mode  g2-gpu-rtx4000a1-m  0.67 USD an hour" in (
        re.sub(r"\x1b\[[0-9;]*m", "", done.stderr)
    )
    assert "make tunnel     in another terminal" in done.stderr
    no_secret_in_any_argument(box)
    assert TOKEN in (box.fake / "curl-stdin").read_text()


def test_the_node_gets_the_env_node_env_writes(tmp_path) -> None:
    box = new_node(tmp_path, DEPLOYABLE + "GPU_COUNT=2\nRETENTION_HOURS=6\n")
    done = box.run("deploy.sh", FAKE_GPUS="2")
    assert done.returncode == 0, done.stderr
    sent = (box.fake / "node.env").read_text().splitlines()
    assert f"CARD_API_KEY={KEY}" in sent
    assert "LLM_RAN_ON=akamai-gpu1" in sent
    assert "RETENTION_HOURS=6" in sent
    assert "ADMIN_CIDR=203.0.113.4/32" not in sent


def test_a_first_deploy_makes_the_key_and_the_node_gets_that_key(tmp_path) -> None:
    """With no key in .env yet, deploy makes one, writes it in .env, and the
    node's .env carries the same one. A key made in a subshell would reach
    neither the node nor the summary."""
    box = new_node(tmp_path, "ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY=\nADMIN_CIDR=203.0.113.4/32\n")
    done = box.run("deploy.sh")
    assert done.returncode == 0, done.stderr
    made = "r" * 64
    assert "CARD_API_KEY generated, in .env" in done.stderr
    assert f"CARD_API_KEY={made}" in (box.repo / ".env").read_text().splitlines()
    assert f"CARD_API_KEY={made}" in (box.fake / "node.env").read_text().splitlines()
    assert made not in done.stdout + done.stderr


def test_terraform_gets_the_settings_from_dotenv(tmp_path) -> None:
    box = Box(
        tmp_path,
        DEPLOYABLE
        + "GPU_COUNT=2\nLINODE_TYPE=g2-gpu-rtx4000a2-m\nLINODE_REGION=fr-par\n"
        + "DOMAIN=cards.example.com\n",
    )
    box.run("deploy.sh", FAKE_SSH_DOWN="1")
    tfvars = box.tfvars()
    assert 'region         = "fr-par"' in tfvars
    assert "gpu_count      = 2\n" in tfvars
    assert 'instance_type  = "g2-gpu-rtx4000a2-m"' in tfvars
    assert 'domain         = "cards.example.com"' in tfvars
    assert 'admin_cidrs    = ["203.0.113.4/32"]' in tfvars
    pub = (box.repo / "secrets" / "ssh" / "id_ed25519.pub").read_text().strip()
    assert f'ssh_public_key = "{pub}"' in tfvars


def test_no_admin_cidr_asks_the_echo_and_uses_a_32(tmp_path) -> None:
    box = Box(tmp_path, f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\n")
    done = box.run("deploy.sh", FAKE_SSH_DOWN="1", FAKE_MY_IP="198.51.100.23")
    assert 'admin_cidrs    = ["198.51.100.23/32"]' in box.tfvars()
    assert "ADMIN_CIDR 198.51.100.23/32, detected" in done.stderr
    assert any("curl -4 -sf --max-time 10 https://whatismyip.akamai.com" in c for c in box.calls)


def test_a_pinned_admin_cidr_that_leaves_this_machine_out_stops_it_before_terraform(
    tmp_path,
) -> None:
    """Every ssh in deploy.sh comes from the machine running it. A pinned
    ADMIN_CIDR that does not hold that machine is a firewall against the script
    itself, and on 2026-11-07 it cost ten minutes and a billed node. Now it is
    a sentence before terraform runs."""
    box = Box(tmp_path, DEPLOYABLE)
    done = box.run("deploy.sh", FAKE_MY_IP="198.51.100.23")
    assert done.returncode != 0
    assert (
        "ADMIN_CIDR in .env is 203.0.113.4/32, and this machine's public IPv4 address is "
        "198.51.100.23" in done.stderr
    )
    assert "No block in it holds 198.51.100.23" in done.stderr
    assert "Add 198.51.100.23/32 to ADMIN_CIDR, or set ADMIN_CIDR= empty" in done.stderr
    assert "Nothing was created" in done.stderr
    assert box.called("terraform") == []


def test_a_pinned_block_that_holds_this_machine_is_used_as_written(tmp_path) -> None:
    box = Box(tmp_path, f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\nADMIN_CIDR=203.0.113.0/24\n")
    done = box.run("deploy.sh", FAKE_SSH_DOWN="1", FAKE_MY_IP="203.0.113.77")
    assert 'admin_cidrs    = ["203.0.113.0/24"]' in box.tfvars()
    assert "ADMIN_CIDR 203.0.113.0/24, from .env. This machine is 203.0.113.77, inside" in (
        done.stderr
    )


def test_two_blocks_reach_terraform_in_order_and_this_machine_may_be_in_either(tmp_path) -> None:
    """A laptop at home and the box the deploy runs on both ssh to the node, so
    both go in ADMIN_CIDR, separated by commas. A space after a comma is fine."""
    box = Box(
        tmp_path,
        f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\nADMIN_CIDR=203.0.113.4/32, 198.51.100.7/32\n",
    )
    done = box.run("deploy.sh", FAKE_SSH_DOWN="1", FAKE_MY_IP="198.51.100.7")
    assert 'admin_cidrs    = ["203.0.113.4/32", "198.51.100.7/32"]' in box.tfvars()
    assert "This machine is 198.51.100.7, inside 198.51.100.7/32." in done.stderr


def test_two_blocks_that_both_leave_this_machine_out_stop_it_before_terraform(tmp_path) -> None:
    box = Box(
        tmp_path,
        f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\nADMIN_CIDR=203.0.113.4/32,198.51.100.7/32\n",
    )
    done = box.run("deploy.sh", FAKE_MY_IP="192.0.2.99")
    assert done.returncode != 0
    assert "No block in it holds 192.0.2.99" in done.stderr
    assert "Add 192.0.2.99/32 to ADMIN_CIDR" in done.stderr
    assert box.called("terraform") == []


def test_an_empty_entry_between_commas_stops_it_before_terraform(tmp_path) -> None:
    box = Box(
        tmp_path,
        f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\nADMIN_CIDR=203.0.113.4/32,,198.51.100.7/32\n",
    )
    done = box.run("deploy.sh")
    assert done.returncode != 0
    assert "ADMIN_CIDR in .env has an empty entry between two commas" in done.stderr
    assert box.called("terraform") == []


def test_a_pinned_admin_cidr_stands_when_the_echo_does_not_answer(tmp_path) -> None:
    """An echo that is down must not stop a deploy whose address is pinned. It
    says what it could not check, and goes on."""
    box = Box(tmp_path, DEPLOYABLE)
    done = box.run("deploy.sh", FAKE_SSH_DOWN="1", FAKE_MY_IP="")
    assert 'admin_cidrs    = ["203.0.113.4/32"]' in box.tfvars()
    assert "could not check ADMIN_CIDR against this machine's address" in done.stderr
    assert box.called("terraform") != []


@pytest.mark.parametrize(
    ("pinned", "bad"),
    [
        ("203.0.113.4", "203.0.113.4"),
        ("203.0.113.4/33", "203.0.113.4/33"),
        ("example.com/32", "example.com/32"),
        ("203.0.113.4/32,example.com/32", "example.com/32"),
    ],
)
def test_a_pinned_admin_cidr_that_is_not_a_block_stops_it_before_terraform(
    tmp_path, pinned, bad
) -> None:
    box = Box(tmp_path, f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\nADMIN_CIDR={pinned}\n")
    done = box.run("deploy.sh")
    assert done.returncode != 0
    assert f"ADMIN_CIDR in .env holds {bad}, which is not an IPv4 CIDR block" in done.stderr
    assert box.called("terraform") == []


@pytest.mark.parametrize("answer", ["", "<html>busy</html>", "2001:db8::1"])
def test_an_echo_that_is_not_an_ipv4_address_stops_it_before_terraform(tmp_path, answer) -> None:
    box = Box(tmp_path, f"ALLOW_CLOUD_DEPLOY=1\nCARD_API_KEY={KEY}\n")
    done = box.run("deploy.sh", FAKE_MY_IP=answer)
    assert done.returncode != 0
    assert "could not find this machine's public IPv4 address. Set ADMIN_CIDR" in done.stderr
    assert box.called("terraform") == []


@pytest.mark.parametrize("name", ["LINODE_REGION", "DOMAIN", "ADMIN_CIDR", "LINODE_TYPE"])
def test_a_setting_that_would_break_the_tfvars_stops_it(tmp_path, name) -> None:
    box = Box(tmp_path, DEPLOYABLE)
    done = box.run("deploy.sh", **{name: 'us-ord"\nimage = "evil'})
    assert done.returncode != 0
    assert f"{name} has a character this script cannot pass on safely" in done.stderr
    assert box.called("terraform") == []


def test_the_copy_leaves_behind_what_git_ignores(tmp_path) -> None:
    """.env, secrets/, the terraform state and the weights stay on the laptop.
    rsync reads the same list git does."""
    box = new_node(tmp_path)
    box.run("deploy.sh")
    (call,) = box.called("rsync")
    assert "--delete" in call.split()
    assert "--exclude=.git/" in call.split()
    assert f"--exclude-from={box.repo}/.gitignore" in call.split()
    assert call.endswith(f"{box.repo}/ deploy@{IP}:/opt/baseball-card/")


def test_too_few_gpus_stops_it_before_the_copy(tmp_path) -> None:
    box = new_node(tmp_path, DEPLOYABLE + "GPU_COUNT=2\n")
    done = box.run("deploy.sh", FAKE_GPUS="1")
    assert done.returncode != 0
    assert "nvidia-smi lists 1 GPUs, and GPU_COUNT=2 needs 2." in done.stderr
    assert box.called("rsync") == []


def test_domain_mode_says_where_to_point_dns(tmp_path) -> None:
    box = new_node(tmp_path, DEPLOYABLE + "DOMAIN=cards.example.com\n")
    done = box.run("deploy.sh")
    assert done.returncode == 0, done.stderr
    assert f"point DNS at it now: cards.example.com A {IP}." in done.stderr
    assert "https://cards.example.com is the gateway. No tunnel." in done.stderr
    # The node checks its own gateway by name, pinned to its loopback.
    livez = [call for call in box.calls if call.startswith("ssh curl") and "/livez" in call]
    assert livez and "--resolve cards.example.com:443:127.0.0.1" in livez[0]


# ---- make deploy: the host key -------------------------------------------------
# Linode hands addresses out again, so a new node can arrive on an old node's
# address with a different host key. deploy.sh forgets the old key only when
# the instance in terraform's state changed.


def host_line(tmp_path: Path, address: str) -> str:
    """A real known_hosts line for address, with a key made for it."""
    key = tmp_path / f"host-{address}"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    kind, blob = Path(f"{key}.pub").read_text().split()[:2]
    return f"{address} {kind} {blob}\n"


def deploy_with_host_keys(
    tmp_path: Path, *, before, after, apply_out, apply_rc=0, known=None
) -> tuple[subprocess.CompletedProcess, Box]:
    box = Box(tmp_path, DEPLOYABLE, before=before)
    if known is not None:
        box.known_hosts.parent.mkdir(parents=True)
        box.known_hosts.write_text("".join(host_line(tmp_path, a) for a in known))
    (box.fake / "state.after").write_text(state(after))
    (box.fake / "apply.out").write_text(apply_out)
    (box.fake / "apply.rc").write_text(str(apply_rc))
    # One try at ssh, then the run stops. The host key is settled by then.
    return box.run("deploy.sh", FAKE_SSH_DOWN="1"), box


def knows(box: Box, address: str) -> bool:
    if not box.known_hosts.exists():
        return False
    return any(line.startswith(f"{address} ") for line in box.known_hosts.read_text().splitlines())


def test_a_rerun_against_the_same_node_keeps_the_key_it_learned(tmp_path) -> None:
    done, box = deploy_with_host_keys(
        tmp_path, before="111", after="111", apply_out=NO_CHANGES, known=[IP, OTHER]
    )
    assert NO_CHANGES in done.stdout, done.stderr
    assert knows(box, IP) and knows(box, OTHER)
    assert "forgot" not in done.stderr
    assert "ssh with the old key known" in box.calls


def test_a_new_node_on_an_old_nodes_address_forgets_the_old_key(tmp_path) -> None:
    done, box = deploy_with_host_keys(
        tmp_path, before=None, after="222", apply_out=CREATED, known=[IP, OTHER]
    )
    assert not knows(box, IP)
    assert knows(box, OTHER), "only the node's own address is forgotten"
    assert f"forgot the host key for {IP}" in done.stderr
    # Forgotten before the first connection, not after it.
    assert "ssh with no key known" in box.calls
    assert "ssh with the old key known" not in box.calls


def test_a_replaced_node_forgets_the_key_of_the_one_it_replaced(tmp_path) -> None:
    done, box = deploy_with_host_keys(
        tmp_path, before="111", after="333", apply_out=REPLACED, known=[IP]
    )
    assert not knows(box, IP)
    assert f"forgot the host key for {IP}" in done.stderr


def test_a_first_deploy_has_nothing_to_forget(tmp_path) -> None:
    done, box = deploy_with_host_keys(tmp_path, before=None, after="222", apply_out=CREATED)
    assert not box.known_hosts.exists()
    assert "forgot" not in done.stderr
    assert f"no ssh on {IP}" in done.stderr


def test_an_apply_that_fails_after_making_the_node_still_forgets(tmp_path) -> None:
    """The instance is in the state though the apply failed later, so the next
    run sees no change. This run has to be the one that forgets."""
    done, box = deploy_with_host_keys(
        tmp_path, before=None, after="222", apply_out=FAILED_AFTER_CREATE, apply_rc=1, known=[IP]
    )
    assert done.returncode != 0
    assert "terraform apply failed" in done.stderr
    assert not knows(box, IP)
    assert box.called("ssh") == []


def test_an_apply_that_fails_before_making_anything_forgets_nothing(tmp_path) -> None:
    done, box = deploy_with_host_keys(
        tmp_path, before="111", after="111", apply_out="Error: 401\n", apply_rc=1, known=[IP]
    )
    assert done.returncode != 0
    assert knows(box, IP)
    assert "forgot" not in done.stderr


# ---- make smoke -----------------------------------------------------------------


def step(name: str, ran_on: str, status: str = "done") -> dict:
    return {"step": name, "status": status, "model": f"m/{name}", "ran_on": ran_on, "ms": 10}


GPU_STEPS = [
    step("cutout", "akamai-gpu0"),
    step("art", "akamai-gpu0"),
    step("comps", "akamai-cpu"),
    step("text", "akamai-gpu0"),
    step("compose", "akamai-cpu"),
    step("render", "akamai-cpu"),
]


def smoke(tmp_path: Path, steps: list[dict], size=(1500, 2100)) -> subprocess.CompletedProcess:
    box = Box(tmp_path, f"CARD_API_KEY={KEY}\n")
    (box.fake / "ledger.json").write_text(json.dumps({"card_id": "abc", "steps": steps}))
    (box.fake / "card.png").write_bytes(png_header(*size))
    photo = box.repo / "me.jpg"
    photo.write_bytes(b"jpeg")
    stats = box.repo / "stats.json"
    stats.write_text('{"name": "Sam"}')
    return box.run("smoke.sh", PHOTO=str(photo), STATS=str(stats), OUT=str(tmp_path / "out"))


def test_a_card_made_on_the_gpus_passes(tmp_path) -> None:
    done = smoke(tmp_path, GPU_STEPS)
    assert done.returncode == 0, done.stderr
    assert "smoke test passed" in done.stderr
    for name in ("cutout", "art", "text"):
        assert f"{name} ran on akamai-gpu0" in done.stderr


def test_a_stand_in_on_the_cpu_fails_the_smoke_test(tmp_path) -> None:
    """The check this script exists for. The mock stack's labels fail it too."""
    steps = [*GPU_STEPS]
    steps[1] = step("art", "akamai-cpu")
    done = smoke(tmp_path, steps)
    assert done.returncode != 0
    assert "art says it ran on akamai-cpu. It has to start with akamai-gpu" in done.stderr


def test_a_step_that_failed_fails_with_its_reason(tmp_path) -> None:
    steps = [*GPU_STEPS]
    steps[3] = {**step("text", "akamai-gpu0", status="failed"), "error": "an invented number"}
    done = smoke(tmp_path, steps)
    assert done.returncode != 0
    assert "the text step is failed, not done" in done.stderr
    assert "an invented number" in done.stderr


def test_a_missing_step_fails(tmp_path) -> None:
    done = smoke(tmp_path, [s for s in GPU_STEPS if s["step"] != "cutout"])
    assert done.returncode != 0
    assert "the cutout step is missing, not done" in done.stderr


def test_a_card_at_the_wrong_size_fails(tmp_path) -> None:
    done = smoke(tmp_path, GPU_STEPS, size=(750, 1050))
    assert done.returncode != 0
    assert "card-front.png is 750x1050, wanted 1500x2100" in done.stderr


def test_smoke_insists_on_the_steps_the_card_claims_for_a_gpu() -> None:
    """card/render.py:MODEL_STEPS decides when the back of the card says it was
    made on Akamai Cloud GPUs. smoke.sh must check the same steps."""
    from card.render import MODEL_STEPS

    source = (DEPLOY / "scripts" / "smoke.sh").read_text()
    listed = re.search(r'^MODEL_STEPS="([^"]+)"', source, re.M).group(1).split()
    assert tuple(listed) == MODEL_STEPS


# ---- make destroy -------------------------------------------------------------------


def test_destroy_refuses_without_the_flag_and_runs_nothing(tmp_path) -> None:
    box = Box(tmp_path, f"ALLOW_CLOUD_DEPLOY=0\nCARD_API_KEY={KEY}\n", before="111")
    done = box.run("destroy.sh", FORCE="1")
    assert done.returncode != 0
    assert box.calls == []


def test_destroy_asks_for_the_label_and_a_wrong_answer_deletes_nothing(tmp_path) -> None:
    box = Box(tmp_path, DEPLOYABLE, before="111")
    (box.repo / "deploy" / "terraform" / "deploy.auto.tfvars").write_text("")
    done = box.run("destroy.sh", stdin="something-else\n")
    assert done.returncode != 0
    assert "Type the instance label to confirm" in done.stderr
    assert "that was not baseball-card. Nothing was deleted." in done.stderr
    assert not [call for call in box.called("terraform") if " destroy " in f"{call} "]


def test_destroy_with_the_label_deletes_and_then_asks_the_api(tmp_path) -> None:
    box = Box(tmp_path, DEPLOYABLE, before="111")
    (box.repo / "deploy" / "terraform" / "deploy.auto.tfvars").write_text("")
    done = box.run("destroy.sh", stdin="baseball-card\n")
    assert done.returncode == 0, done.stderr
    destroys = [call for call in box.called("terraform") if call.split()[2:3] == ["destroy"]]
    assert len(destroys) == 1
    assert "nothing tagged baseball-card is left. The meter is stopped." in done.stderr
    no_secret_in_any_argument(box)


def test_force_destroys_without_asking(tmp_path) -> None:
    box = Box(tmp_path, DEPLOYABLE, before="111")
    (box.repo / "deploy" / "terraform" / "deploy.auto.tfvars").write_text("")
    done = box.run("destroy.sh", FORCE="1")
    assert done.returncode == 0, done.stderr
    assert "Type the instance label" not in done.stderr


def test_the_check_asks_for_the_exact_tag_and_counts_only_rows_with_it(tmp_path) -> None:
    """The API filters by tag. Each row is checked again for the exact tag, so a
    resource with a longer tag that starts the same way is never counted, even
    if a filter ever matched by prefix."""
    box = Box(tmp_path, DEPLOYABLE)
    box.answer("instances", [{"id": 7, "label": "someone-elses", "tags": ["baseball-card-old"]}])
    done = box.run("destroy.sh")
    assert done.returncode == 0, done.stderr
    filters = [call for call in box.called("curl") if "X-Filter" in call]
    assert len(filters) == 2
    assert all('X-Filter: {"tags": "baseball-card"}' in call for call in filters)
    assert "someone-elses" not in done.stderr


def test_a_leftover_with_the_tag_fails_and_names_it(tmp_path) -> None:
    box = Box(tmp_path, DEPLOYABLE)
    box.answer("instances", [{"id": 42, "label": "baseball-card", "tags": ["baseball-card"]}])
    done = box.run("destroy.sh")
    assert done.returncode != 0
    assert "42  baseball-card" in done.stderr
    assert "1 resource(s) tagged baseball-card may still be there" in done.stderr


# ---- make rotate-key -----------------------------------------------------------------


def rotate(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, Box]:
    box = Box(tmp_path, "GATEWAY_PORT=8445\n" + DEPLOYABLE + "LOG_LEVEL=INFO\n", before="111")
    (box.fake / "active").write_text(KEY + "\n")
    (box.fake / "node_key").write_text(KEY + "\n")
    return box.run("rotate-key.sh", **env), box


def test_rotate_key_refuses_without_the_flag(tmp_path) -> None:
    box = Box(tmp_path, f"CARD_API_KEY={KEY}\n", before="111")
    done = box.run("rotate-key.sh")
    assert done.returncode != 0
    assert box.calls == []


def test_a_rotation_writes_both_places_and_proves_the_old_key_dead(tmp_path) -> None:
    done, box = rotate(tmp_path)
    assert done.returncode == 0, done.stderr
    new = (box.fake / "node_key").read_text().strip()
    assert new == "r" * 64 and new != KEY
    lines = (box.repo / ".env").read_text().splitlines()
    # In place: the key's line where it was, and nothing else moved.
    assert lines == [
        "GATEWAY_PORT=8445",
        "ALLOW_CLOUD_DEPLOY=1",
        f"CARD_API_KEY={new}",
        "ADMIN_CIDR=203.0.113.4/32",
        "LOG_LEVEL=INFO",
    ]
    assert "the old key gets 401 from the api" in done.stderr
    assert "the old key gets 401 from vLLM too" in done.stderr
    assert "ssh cd /opt/baseball-card && docker compose up -d" in box.calls
    no_secret_in_any_argument(box)
    assert new not in "\n".join(box.calls)


def test_a_node_that_still_takes_the_old_key_fails_the_rotation(tmp_path) -> None:
    """The case the proof exists for: a container that was not recreated."""
    done, box = rotate(tmp_path, FAKE_KEEP_OLD="1")
    assert done.returncode != 0
    assert "the old key still gets 200 from /healthz. It has not been revoked." in done.stderr
    # The node answers to the new key, so .env holds it: nobody is locked out.
    assert f"CARD_API_KEY={'r' * 64}" in (box.repo / ".env").read_text()


def test_a_key_that_already_fails_is_not_rotated(tmp_path) -> None:
    box = Box(tmp_path, DEPLOYABLE, before="111")
    (box.fake / "active").write_text("some-other-key\n")
    done = box.run("rotate-key.sh")
    assert done.returncode != 0
    assert "the current key gets 401 from /healthz already" in done.stderr
    assert not (box.fake / "node_key").exists()


# ---- make tunnel and make ssh ---------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_tunnel_forwards_gateway_port_to_the_nodes_loopback(tmp_path) -> None:
    port = free_port()
    box = Box(tmp_path, f"CARD_API_KEY={KEY}\nGATEWAY_PORT={port}\n", before="111")
    done = box.run("tunnel.sh")
    assert done.returncode == 0, done.stderr
    args = (box.fake / "ssh-args").read_text().split()
    assert args[:1] == ["-N"]
    assert args[args.index("-L") + 1] == f"{port}:127.0.0.1:8443"
    assert args[args.index("-i") + 1] == str(box.repo / "secrets" / "ssh" / "id_ed25519")
    assert f"UserKnownHostsFile={box.known_hosts}" in args
    assert "ExitOnForwardFailure=yes" in args
    assert args[-1] == f"deploy@{IP}"


@pytest.mark.skipif(shutil.which("ss") is None, reason="no ss here to look for a listener")
def test_tunnel_refuses_a_port_something_already_listens_on(tmp_path) -> None:
    """The mock stack on the same port, say. A forward would fail to bind."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        box = Box(tmp_path, f"CARD_API_KEY={KEY}\nGATEWAY_PORT={port}\n", before="111")
        done = box.run("tunnel.sh")
    assert done.returncode != 0
    assert f"something is already listening on localhost:{port}" in done.stderr
    assert box.called("ssh") == []


def test_tunnel_without_a_node_says_to_deploy(tmp_path) -> None:
    box = Box(tmp_path, f"CARD_API_KEY={KEY}\n")
    done = box.run("tunnel.sh")
    assert done.returncode != 0
    assert "no terraform state, so there is no node. Run make deploy first." in done.stderr


def test_tunnel_in_domain_mode_says_there_is_nothing_to_tunnel_to(tmp_path) -> None:
    box = Box(tmp_path, f"CARD_API_KEY={KEY}\nDOMAIN=cards.example.com\n", before="111")
    done = box.run("tunnel.sh")
    assert done.returncode != 0
    assert "https://cards.example.com is the gateway" in done.stderr
    assert box.called("ssh") == []


def test_gpu_logs_follow_compose_once_the_node_is_ready(tmp_path) -> None:
    box = Box(tmp_path, f"CARD_API_KEY={KEY}\n", before="111")
    done = box.run("ssh.sh", "logs")
    assert done.returncode == 0, done.stderr
    args = (box.fake / "ssh-args").read_text()
    assert args.startswith("-t ")
    assert "if [ -f /var/lib/baseball-card/ready ]" in args
    assert "docker compose logs -f --tail=100" in args


# ---- facts the scripts share with the rest of the repo ------------------------------


def test_the_base_image_the_node_builds_is_the_one_the_real_images_start_from() -> None:
    deploy = (DEPLOY / "scripts" / "deploy.sh").read_text()
    built = re.search(r"-t (baseball-card-vision-base:\S+) \.", deploy).group(1)
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert f"VISION_BASE := {built}" in makefile
    for service in ("cutout", "art"):
        dockerfile = (REPO_ROOT / "services" / service / "Dockerfile.real").read_text()
        assert f"ARG VISION_BASE={built}" in dockerfile


def test_gitignore_holds_no_negation_rsync_would_read_another_way() -> None:
    """deploy.sh hands .gitignore to rsync as its exclude list. rsync and git read
    plain patterns the same way, but not a line that starts with !."""
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert not [line for line in lines if line.startswith("!")]
    for kept_back in (".env", "secrets/", "*.tfstate", "*.tfvars", ".terraform/", "out/"):
        assert kept_back in lines


def test_the_targets_the_page_names_exist() -> None:
    """The app's sentences tell a person to run make tunnel and make rotate-key."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    pages = "\n".join(p.read_text() for p in (REPO_ROOT / "web" / "app").glob("*.js"))
    for target in ("tunnel", "rotate-key"):
        assert f"make {target}" in pages
        assert re.search(rf"^{target}: ##", makefile, re.M), target


@pytest.mark.parametrize("script", sorted(p.name for p in (DEPLOY / "scripts").glob("*.sh")))
def test_every_script_but_lib_runs_on_its_own_and_lib_is_only_sourced(script: str) -> None:
    path = DEPLOY / "scripts" / script
    runnable = os.access(path, os.X_OK)
    assert runnable is (script != "lib.sh"), script
    if script != "lib.sh":
        assert path.read_text().startswith("#!/usr/bin/env bash\n")
        assert "set -euo pipefail" in path.read_text()
