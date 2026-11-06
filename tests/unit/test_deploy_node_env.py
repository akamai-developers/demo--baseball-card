"""The node's .env, which deploy/scripts/lib.sh's node_env writes.

It is the only file that carries a secret onto the node, and the one place a
laptop's settings become the node's. Each test runs node_env in a throwaway
copy of deploy/, with a made-up key and only the environment the test gives,
so nothing of this box's own .env can leak in. Then compose renders the result,
the way it reads the file on the node.

The rest checks .env.example against what the deploy scripts and the services
read: every name it sets is read by something, every setting a script or a
service reads is in it, and block 3 lists every line the node's .env holds.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from common.settings import Settings
from tests.conftest import REPO_ROOT, refute_key

DEPLOY = REPO_ROOT / "deploy"
LIB_SH = DEPLOY / "scripts" / "lib.sh"
LAYOUTS = DEPLOY / "layouts"
EXAMPLE = REPO_ROOT / ".env.example"

# A stand-in for CARD_API_KEY. Never the real one, and not hex, so the secret
# scan does not read it as a key.
KEY = "k" * 64


def env_lines(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def run_node_env(tmp_path: Path, dotenv: str = "", **env: str) -> subprocess.CompletedProcess:
    """node_env in a copy of deploy/ with `dotenv` as the repo's .env."""
    repo = tmp_path / "repo"
    (repo / "deploy" / "scripts").mkdir(parents=True)
    shutil.copy(LIB_SH, repo / "deploy" / "scripts" / "lib.sh")
    shutil.copy(DEPLOY / "models.env", repo / "deploy" / "models.env")
    shutil.copytree(LAYOUTS, repo / "deploy" / "layouts")
    (repo / ".env").write_text(dotenv)
    return subprocess.run(
        ["bash", "-c", ". deploy/scripts/lib.sh && node_env"],
        cwd=repo,
        env={"PATH": os.environ["PATH"], "CARD_API_KEY": KEY, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def node_env(tmp_path: Path, dotenv: str = "", **env: str) -> dict[str, str]:
    """The node's .env as NAME -> value, comments left out."""
    done = run_node_env(tmp_path, dotenv, **env)
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line and not line.startswith("#")]
    names = [line.split("=", 1)[0] for line in lines]
    assert len(names) == len(set(names)), "a name is written twice"
    return dict(line.split("=", 1) for line in lines)


# ---- what the node gets -------------------------------------------------------


def test_deploy_writes_the_node_env_with_node_env_over_stdin() -> None:
    """The lines are tested through the function, so the script must use it, and
    keep the result off the disk and off any command line."""
    deploy = (DEPLOY / "scripts" / "deploy.sh").read_text()
    assert 'body="$(node_env)"' in deploy
    assert 'printf \'%s\\n\' "$body" | ssh_node "umask 077 && cat >' in deploy


def test_the_node_gets_the_pins_and_the_labels_the_api_cannot_work_out(tmp_path) -> None:
    """vLLM is a stock image. It reports no licence and no card, and without
    LLM_MODEL the api would ask it for the mock model's name and get a 404."""
    sent = node_env(tmp_path)
    pins = env_lines(DEPLOY / "models.env")
    for name in ("LLM_MODEL", "LLM_REVISION", "LLM_LICENSE"):
        assert sent[name] == pins[name]
    assert sent["BACKEND"] == "real"
    # The api's own steps run on the node's CPU, and the ledger says so.
    assert sent["GPU_LABEL"] == "akamai-cpu"
    assert sent["COMPOSE_PROFILES"] == "gpu"


@pytest.mark.parametrize("count", [1, 2])
def test_each_gpu_count_brings_its_own_layout(tmp_path, count: int) -> None:
    sent = node_env(tmp_path, GPU_COUNT=str(count))
    layout = env_lines(LAYOUTS / f"gpu{count}.env")
    assert {name: sent[name] for name in layout} == layout


def test_no_gpu_count_is_the_one_gpu_layout(tmp_path) -> None:
    sent = node_env(tmp_path)
    assert sent["LLM_RAN_ON"] == "akamai-gpu0"
    assert sent["ART_CPU_OFFLOAD"] == "1"


@pytest.mark.parametrize("count", ["3", "0", "two"])
def test_a_gpu_count_with_no_layout_is_refused_before_a_line_is_written(tmp_path, count) -> None:
    done = run_node_env(tmp_path, GPU_COUNT=count)
    assert done.returncode != 0
    assert done.stdout == ""
    assert f"GPU_COUNT is {count}. It is 1 or 2" in done.stderr


def test_the_key_is_one_line_and_nowhere_else(tmp_path) -> None:
    done = run_node_env(tmp_path)
    assert [line for line in done.stdout.splitlines() if KEY in line] == [f"CARD_API_KEY={KEY}"]
    refute_key(done.stderr, KEY, "node_env's messages")


def test_a_key_too_short_to_use_is_refused(tmp_path) -> None:
    """With BACKEND=real every service refuses a key under 32 characters, so a
    node given one would not start."""
    done = run_node_env(tmp_path, CARD_API_KEY="short")
    assert done.returncode != 0
    assert "CARD_API_KEY is shorter than 32 characters" in done.stderr


def test_domain_mode_brings_the_overlay_and_tunnel_mode_does_not(tmp_path) -> None:
    tunnel = node_env(tmp_path / "tunnel")
    assert "COMPOSE_FILE" not in tunnel
    assert tunnel["DOMAIN"] == ""
    domain = node_env(tmp_path / "domain", DOMAIN="cards.example.com")
    assert domain["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.domain.yml"
    assert domain["DOMAIN"] == "cards.example.com"


def test_hf_token_comes_from_dotenv_and_never_from_the_shell(tmp_path) -> None:
    """A shell profile may export HF_TOKEN for other work. The node needs none
    for the pinned models, so only the one written in .env goes."""
    shell_only = node_env(tmp_path / "a", HF_TOKEN="from-the-shell")
    assert shell_only["HF_TOKEN"] == ""
    both = node_env(tmp_path / "b", dotenv="HF_TOKEN=from-dotenv\n", HF_TOKEN="from-the-shell")
    assert both["HF_TOKEN"] == "from-dotenv"


def test_block_two_reaches_the_node_as_the_laptop_has_it(tmp_path) -> None:
    sent = node_env(tmp_path, RETENTION_HOURS="1", ART_POOL_SIZE="5")
    assert sent["RETENTION_HOURS"] == "1"
    assert sent["ART_POOL_SIZE"] == "5"
    # A default the laptop does not set is left to the service's own default.
    assert "SWEEP_EVERY_S" not in sent


def test_the_laptops_gateway_port_stays_on_the_laptop(tmp_path) -> None:
    """On the node Caddy binds 127.0.0.1:8443, where make tunnel forwards to.
    GATEWAY_PORT is this laptop's side of the forward."""
    sent = node_env(tmp_path, GATEWAY_PORT="8445", ADMIN_CIDR="203.0.113.4/32")
    assert "GATEWAY_PORT" not in sent
    assert "ADMIN_CIDR" not in sent
    assert "ALLOW_CLOUD_DEPLOY" not in sent


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOMAIN", "cards.example.com; reboot"),
        ("LOG_LEVEL", "INFO DEBUG"),
        ("RETENTION_HOURS", "$(id)"),
        ("HF_TOKEN", "a'quote"),
    ],
)
def test_a_value_the_node_env_cannot_hold_is_refused_by_name(tmp_path, name, value) -> None:
    """Compose and bash both read the file. A value either would read another way
    stops the deploy, and the message names the setting, not its value."""
    if name == "HF_TOKEN":
        done = run_node_env(tmp_path, dotenv=f"HF_TOKEN={value}\n")
    else:
        done = run_node_env(tmp_path, **{name: value})
    assert done.returncode != 0
    assert f"{name} has a character this script cannot pass on safely" in done.stderr
    assert value not in done.stderr


def test_bash_reads_the_node_env_as_compose_does(tmp_path) -> None:
    """The scripts on the node source it with set -a. Every line is a plain
    assignment, so bash and compose see the same values."""
    done = run_node_env(tmp_path, DOMAIN="cards.example.com", GPU_COUNT="2")
    node = tmp_path / "node.env"
    node.write_text(done.stdout)
    read = subprocess.run(
        [
            "bash",
            "-c",
            f'set -a; . "{node}"; set +a; printf "%s|%s|%s" "$COMPOSE_FILE" "$LLM_GPU" "$DOMAIN"',
        ],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert read.stdout == "docker-compose.yml:docker-compose.domain.yml|1|cards.example.com"


# ---- compose, reading the node's .env -----------------------------------------


@pytest.mark.parametrize(
    ("count", "domain"), [(1, ""), (2, ""), (1, "cards.example.com"), (2, "cards.example.com")]
)
def test_compose_on_the_node_runs_the_layout_and_the_mode_the_env_names(
    tmp_path, docker: list[str], count: int, domain: str
) -> None:
    """What `docker compose config` makes of the node's .env, read the way the
    node reads it: as the project's env file, with nothing from this laptop's
    shell. No --profile and no -f: the file names both."""
    done = run_node_env(tmp_path, GPU_COUNT=str(count), DOMAIN=domain)
    node = tmp_path / "node.env"
    node.write_text(done.stdout)
    rendered = subprocess.run(
        [*docker, "compose", "--env-file", str(node), "config", "--format", "json"],
        cwd=REPO_ROOT,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert rendered.returncode == 0, rendered.stderr
    import json

    config = json.loads(rendered.stdout)
    refute_key(rendered.stderr, KEY, "compose's messages")
    services = config["services"]
    assert set(services) == {"api", "caddy", "cutout-gpu", "art-gpu", "llm-gpu", "gpu-stats-gpu"}
    ports = [(p.get("host_ip"), int(p["target"])) for p in services["caddy"]["ports"]]
    if domain:
        assert ports == [(None, 80), (None, 443)]
        assert services["caddy"]["environment"]["CADDY_SITE"] == domain
    else:
        assert ports == [("127.0.0.1", 8443)]
        assert int(services["caddy"]["ports"][0]["published"]) == 8443
    layout = env_lines(LAYOUTS / f"gpu{count}.env")
    devices = services["art-gpu"]["deploy"]["resources"]["reservations"]["devices"][0]
    assert devices["device_ids"] == [layout["ART_GPU"]]
    assert services["api"]["environment"]["LLM_RAN_ON"] == layout["LLM_RAN_ON"]
    assert services["llm-gpu"]["environment"]["VLLM_API_KEY"] == KEY


# ---- .env.example -------------------------------------------------------------


def example_blocks() -> tuple[list[str], list[str], list[str]]:
    """The names in .env.example's three blocks. Blocks 1 and 2 by the lines that
    set them. Block 3, the node's .env, by its comment lines, one per name."""
    text = EXAMPLE.read_text()
    head, three = text.split("\n# 3. The node's .env", 1)
    one, two = head.split("\n# 2. Defaults", 1)
    sets = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.M)
    listed = re.compile(r"^# ([A-Z][A-Z0-9_]+) {2,}\S", re.M)
    return sets.findall(one), sets.findall(two), listed.findall(three)


def test_block_three_lists_every_line_the_node_gets(tmp_path) -> None:
    """With every optional line on: a domain, and all of block 2 set. Block 3
    names each line, block 2 included by its last sentence, no more and no
    fewer, so the list cannot go stale either way."""
    one, two, three = example_blocks()
    defaults = env_lines(EXAMPLE)
    sent = node_env(tmp_path, DOMAIN="cards.example.com", **{name: defaults[name] for name in two})
    assert sorted(sent) == sorted(set(three) | set(two))
    assert len(three) == len(set(three)), "block 3 lists a name twice"
    # Comments only. A line here that set something would land in a laptop's .env.
    block = EXAMPLE.read_text().split("\n# 3. The node's .env", 1)[1].splitlines()[1:]
    assert all(not line or line.startswith("#") for line in block)


def test_the_settings_the_node_takes_from_block_two_are_block_two() -> None:
    lib = LIB_SH.read_text()
    listed = re.search(r'^NODE_SETTINGS="([^"]+)"$', lib, re.M).group(1).split()
    _, two, _ = example_blocks()
    assert listed == two


def test_the_example_env_sets_nothing_that_nothing_reads() -> None:
    """Every name blocks 1 and 2 set is a Settings field, or compose, the Makefile
    or a deploy script reads it."""
    fields = {name.upper() for name in Settings.model_fields}
    readers = [
        REPO_ROOT / "docker-compose.yml",
        REPO_ROOT / "docker-compose.domain.yml",
        REPO_ROOT / "Makefile",
        *sorted((DEPLOY / "scripts").glob("*.sh")),
        DEPLOY / "scripts" / "gpu_check.py",
    ]
    text = "\n".join(path.read_text() for path in readers)
    one, two, _ = example_blocks()
    unread = [
        name
        for name in one + two
        if name not in fields
        and not re.search(rf"\$[{{(]?{name}\b", text)
        and f'"{name}"' not in text
    ]
    assert unread == []


# Read by the deploy scripts and set by none of them, and still not a line of
# .env: the token goes in your shell, FORCE and the photo are for one run, and
# bash keeps the other three itself.
NOT_IN_ENV = {"LINODE_TOKEN", "FORCE", "BASH_REMATCH", "BASH_SOURCE", "SECONDS"}


def test_every_setting_the_deploy_scripts_read_is_in_the_example_env() -> None:
    """load_env puts .env in the scripts' environment. A name they read there and
    never give a value themselves is one a person sets, so block 1 or 2 has it.
    The pins come from deploy/models.env, and the layout from deploy/layouts/."""
    text = "\n".join(path.read_text() for path in sorted((DEPLOY / "scripts").glob("*.sh")))
    read = set(re.findall(r"\$\{?!?([A-Z][A-Z0-9_]*)", text))
    given = set(re.findall(r"(?:^|[\s;(])(?:local |export )?([A-Z][A-Z0-9_]*)=", text, re.M))
    given |= set(re.findall(r"\bfor ([A-Z][A-Z0-9_]*) in\b", text))
    one, two, _ = example_blocks()
    known = set(env_lines(DEPLOY / "models.env")) | set(env_lines(LAYOUTS / "gpu1.env"))
    assert sorted(read - given - known - NOT_IN_ENV - set(one) - set(two)) == []
    assert NOT_IN_ENV.isdisjoint(one + two)


# Settings fields .env.example leaves out: compose sets each per service.
NOT_IN_EXAMPLE = {
    "SERVICE_NAME",
    "DEVICE",
    "DATA_DIR",
    "CUTOUT_URL",
    "ART_URL",
    "LLM_URL",
    "WEB_DIR",
    "MODELS_ENV",
}


def test_the_example_env_names_every_setting_a_service_reads() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    one, two, three = example_blocks()
    assert sorted(fields - NOT_IN_EXAMPLE - set(one + two + three)) == []
    assert NOT_IN_EXAMPLE.issubset(fields), "a field in the exceptions is gone"
