"""The gpu profile, read out of the rendered compose file, in both GPU layouts.

No GPU is needed to check any of this. What these assert is the arrangement:
which card each model lands on for GPU_COUNT=1 and GPU_COUNT=2, that the label
each service reports names that card, that the plain service names still
resolve, and that the api and vLLM agree on the one model name they both have
to know. And the memory sums in docker-compose.yml's comment, from the settings
the rendered file holds, so a fraction for vLLM that does not fit fails here.

The port rules are in tests/security/test_compose_ports.py.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.conftest import (
    REPO_ROOT,
    TEST_KEY,
    compose_config,
    confirm_key,
    dotenv_value,
    refute_key,
)

LAYOUTS = REPO_ROOT / "deploy" / "layouts"
MODELS_ENV = REPO_ROOT / "deploy" / "models.env"
MODEL_SERVICES = ("cutout-gpu", "art-gpu", "llm-gpu")
GPU_SERVICES = (*MODEL_SERVICES, "gpu-stats-gpu")
VLLM_IMAGE = "vllm/vllm-openai:v0.29.0"

# Where each model goes, by GPU_COUNT.
PLACED = {
    1: {"cutout-gpu": ["0"], "art-gpu": ["0"], "llm-gpu": ["0"]},
    2: {"cutout-gpu": ["1"], "art-gpu": ["0"], "llm-gpu": ["1"]},
}

# nvidia-smi on a g2-gpu-rtx4000a2-s node with the same models and settings:
# an RTX 4000 Ada's memory, FLUX alone on a card (September 2026), BiRefNet,
# and what vLLM took beyond its fraction at 0.80, 16,982 - 16,380 (2026-10-30).
CARD_MIB = 20475
FLUX_MIB = 17956
BIREFNET_MIB = 2012
VLLM_OVERSHOOT_MIB = 602
# On paper, not measured: FLUX with its weights in RAM, a layer at a time.
FLUX_STREAMED_MIB = 3000
# What a card must keep free. Fragmentation is what makes a tight fit not fit.
SPARE_MIB = 1024


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines, comments and blanks skipped."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def layout(count: int) -> dict[str, str]:
    return read_env(LAYOUTS / f"gpu{count}.env")


@pytest.fixture(scope="module", params=sorted(PLACED))
def gpus(request) -> int:
    return request.param


@pytest.fixture(scope="module")
def config(docker: list[str], gpus: int) -> dict:
    """The gpu profile as the node renders it: the layout's lines in its .env."""
    return compose_config(docker, ("gpu",), layout(gpus))


def pinned(config: dict) -> dict[str, list[str]]:
    """Each service that reserves particular cards, and which."""
    placed = {}
    for name, svc in config["services"].items():
        devices = svc.get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices")
        if devices and "device_ids" in devices[0]:
            placed[name] = devices[0]["device_ids"]
    return placed


def flags(command: list[str]) -> dict[str, str]:
    """--a b --c d as a dict. Every flag in vLLM's command takes a value."""
    return dict(zip(command[::2], command[1::2], strict=True))


# ---- the layout files -------------------------------------------------------


def test_there_is_a_layout_for_each_gpu_count_and_no_other() -> None:
    assert sorted(path.name for path in LAYOUTS.iterdir()) == [
        f"gpu{count}.env" for count in sorted(PLACED)
    ]


@pytest.mark.parametrize("count", sorted(PLACED))
def test_a_layout_file_is_only_key_equals_value(count: int) -> None:
    """deploy.sh copies these lines into the node's .env, which compose reads."""
    for number, line in enumerate((LAYOUTS / f"gpu{count}.env").read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z0-9._-]+", stripped), (
                f"gpu{count}.env line {number} is not a plain KEY=value: {line!r}"
            )


def test_both_layouts_set_the_same_names() -> None:
    """A name only one layout sets would keep the other layout's value on a
    node that moved from one to the other."""
    assert set(layout(1)) == set(layout(2))


def test_the_defaults_in_compose_are_the_one_gpu_layout(docker: list[str]) -> None:
    """A node .env without the layout's lines runs GPU_COUNT=1, as the default."""
    bare = compose_config(docker, ("gpu",))
    one = compose_config(docker, ("gpu",), layout(1))
    for name in GPU_SERVICES:
        assert bare["services"][name] == one["services"][name], name


# ---- what is open -------------------------------------------------------------


def test_only_caddy_publishes_a_port_and_on_loopback(config: dict) -> None:
    """In each layout, as tests/security/test_compose_ports.py checks the
    default one. The GPU services publish nothing."""
    publishing = {name for name, svc in config["services"].items() if svc.get("ports")}
    assert publishing == {"caddy"}
    ports = config["services"]["caddy"]["ports"]
    assert [(port.get("host_ip"), int(port["target"])) for port in ports] == [("127.0.0.1", 8443)]


# ---- where each model goes --------------------------------------------------


def test_each_model_is_on_the_card_its_layout_gives_it(config: dict, gpus: int) -> None:
    """One GPU: all three on card 0. Two: FLUX alone on 0, vLLM and BiRefNet on 1."""
    assert pinned(config) == PLACED[gpus]


def test_every_gpu_service_says_the_card_it_is_pinned_to(config: dict, gpus: int) -> None:
    """The label goes on every answer and onto the back of the card. It has to
    name the card compose gave the container. vLLM reads no label: the api is
    told, through LLM_RAN_ON in the node's .env."""
    for name, devices in pinned(config).items():
        if name == "llm-gpu":
            label = config["services"]["api"]["environment"]["LLM_RAN_ON"]
        else:
            label = config["services"][name]["environment"]["GPU_LABEL"]
        assert label == f"akamai-gpu{devices[0]}", name


def test_flux_streams_from_ram_on_one_card_and_not_on_two(config: dict, gpus: int) -> None:
    offload = config["services"]["art-gpu"]["environment"]["ART_CPU_OFFLOAD"]
    assert offload == {1: "1", 2: "0"}[gpus]


def test_each_card_has_room_to_spare(config: dict, gpus: int) -> None:
    """The sums in docker-compose.yml's comment, from what this layout renders.

    vLLM takes its fraction of the card, and nvidia-smi showed it take 602 MiB
    more than that at 0.80. The two-GPU sums are measured numbers. The one-GPU
    sum has FLUX's streamed figure in it, which is on paper.
    """
    services = config["services"]
    fraction = float(flags(services["llm-gpu"]["command"])["--gpu-memory-utilization"])
    streamed = services["art-gpu"]["environment"]["ART_CPU_OFFLOAD"] == "1"
    on_card: dict[str, int] = {}
    use = {
        "llm-gpu": fraction * CARD_MIB + VLLM_OVERSHOOT_MIB,
        "cutout-gpu": BIREFNET_MIB,
        "art-gpu": FLUX_STREAMED_MIB if streamed else FLUX_MIB,
    }
    for name, devices in pinned(config).items():
        on_card[devices[0]] = on_card.get(devices[0], 0) + use[name]
    for card, used in on_card.items():
        assert CARD_MIB - used > SPARE_MIB, f"card {card} would hold {used:.0f} MiB"


def test_vllm_cannot_take_less_of_the_card_than_its_weights(config: dict) -> None:
    """Gemma 4 12B is 9,789 MiB of weights, and vLLM needs a KV cache besides."""
    fraction = float(flags(config["services"]["llm-gpu"]["command"])["--gpu-memory-utilization"])
    assert fraction * CARD_MIB > 9789 + 1024


def test_gpu_stats_can_see_every_card_and_holds_none(config: dict) -> None:
    """It runs nvidia-smi, so it needs every card and the utility capability. It
    loads no model, so it pins no device."""
    reservation = config["services"]["gpu-stats-gpu"]["deploy"]["resources"]["reservations"]
    device = reservation["devices"][0]
    # compose renders `count: all` as -1.
    assert device.get("count") == -1
    assert "device_ids" not in device
    assert "utility" in device["capabilities"]


def test_every_gpu_service_answers_to_its_plain_name(config: dict) -> None:
    """The Caddyfile and the api route to cutout, art, llm and gpu-stats in both
    profiles."""
    aliases = {
        name: (svc.get("networks", {}).get("card") or {}).get("aliases")
        for name, svc in config["services"].items()
        if (svc.get("networks", {}).get("card") or {}).get("aliases")
    }
    assert aliases == {
        "cutout-gpu": ["cutout"],
        "art-gpu": ["art"],
        "llm-gpu": ["llm"],
        "gpu-stats-gpu": ["gpu-stats"],
    }


def test_the_weights_volume_is_shared_by_all_three_model_services(config: dict) -> None:
    """BiRefNet, FLUX and Gemma, downloaded once."""
    for name in MODEL_SERVICES:
        sources = {mount["source"] for mount in config["services"][name]["volumes"]}
        assert "hf-cache" in sources, f"{name} would download its weights again"


def test_the_art_service_keeps_its_pool_on_a_volume(config: dict, docker: list[str]) -> None:
    """The backgrounds it painted ahead outlive a restart of the container."""
    mock = compose_config(docker, ("mock",))

    def data_volume(svc: dict) -> str:
        return next(v["source"] for v in svc["volumes"] if v["target"] == "/data")

    assert data_volume(config["services"]["art-gpu"]) == "art-data"
    assert data_volume(mock["services"]["art"]) == "art-data"


# ---- vLLM ---------------------------------------------------------------------


def test_vllm_runs_the_stock_image_with_nothing_of_ours_in_it(config: dict) -> None:
    llm = config["services"]["llm-gpu"]
    assert llm["image"] == VLLM_IMAGE
    assert "build" not in llm


def test_vllm_serves_the_pinned_checkpoint(config: dict) -> None:
    command = flags(config["services"]["llm-gpu"]["command"])
    pins = read_env(MODELS_ENV)
    assert command["--model"] == command["--served-model-name"] == pins["LLM_MODEL"]
    assert command["--revision"] == pins["LLM_REVISION"]
    assert command["--max-model-len"] == "8192"
    assert command["--port"] == "8000"


def test_vllm_gets_the_key_and_nothing_else_from_env(config: dict) -> None:
    """VLLM_API_KEY is CARD_API_KEY, as an environment variable and never a flag.
    No env_file: vLLM is handed four names, not the whole .env."""
    llm = config["services"]["llm-gpu"]
    assert sorted(llm["environment"]) == [
        "HF_HOME",
        "HF_TOKEN",
        "VLLM_API_KEY",
        "VLLM_NO_USAGE_STATS",
    ]
    # What compose read: the shell's key, else the one in .env, else the stand-in.
    key = os.environ.get("CARD_API_KEY") or dotenv_value("CARD_API_KEY") or TEST_KEY
    confirm_key(llm["environment"]["VLLM_API_KEY"], key, "vLLM's VLLM_API_KEY")
    assert "--api-key" not in llm["command"]
    refute_key(" ".join(llm["command"]), key, "vLLM's command line")


def test_vllm_sends_no_usage_report(config: dict) -> None:
    """Without this vLLM reports the hardware, its settings and the model's
    architecture to stats.vllm.ai. Nothing leaves the node."""
    assert config["services"]["llm-gpu"]["environment"]["VLLM_NO_USAGE_STATS"] == "1"


def test_compose_refuses_to_start_vllm_without_a_key(docker: list[str]) -> None:
    """An empty VLLM_API_KEY would serve /v1 to anyone who reaches it.

    --env-file /dev/null, so this box's .env cannot supply the key.
    """
    env = {name: value for name, value in os.environ.items() if name != "CARD_API_KEY"}
    result = subprocess.run(
        [*docker, "compose", "--env-file", "/dev/null", "--profile", "gpu", "config"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 0, "compose rendered vLLM with no key"
    assert "CARD_API_KEY is empty. Run make env" in result.stderr


def test_the_api_and_vllm_agree_on_the_model_name(docker: list[str]) -> None:
    """The api puts LLM_MODEL in every request. vLLM 404s a name it does not serve."""
    chosen = "org/some-other-gemma"
    config = compose_config(docker, ("gpu",), {"LLM_MODEL": chosen})
    command = flags(config["services"]["llm-gpu"]["command"])

    assert config["services"]["api"]["environment"]["LLM_MODEL"] == chosen
    assert command["--model"] == command["--served-model-name"] == chosen


def test_vllm_is_not_healthy_until_the_model_is_served(config: dict) -> None:
    health = config["services"]["llm-gpu"]["healthcheck"]
    # vLLM answers /health only once the engine is up. 9.6 GiB off a volume and
    # onto a card, plus CUDA graph capture, is minutes.
    assert any("/health'" in part for part in health["test"])
    # compose renders a duration as a string. 900 s of grace before a slow start
    # counts as a failure and the container is restarted into the same slow start.
    assert health["start_period"] == "15m0s"


def test_caddy_routes_none_of_the_paths_vllm_leaves_open() -> None:
    """vLLM's key check guards only /v1. /health and /metrics are open on the
    container, and stay on the internal network. One character apart from the
    api's /healthz, so match whole paths."""
    caddyfile = (REPO_ROOT / "Caddyfile").read_text()
    routed = set(re.findall(r"handle\s+(\S+)\s*\{", caddyfile))
    assert routed == {"/v1/*", "/cutout", "/art/generate", "/gpu"}


# ---- the source file ----------------------------------------------------------


def test_the_gpu_profile_carries_no_mock_service() -> None:
    source = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    for name, svc in source["services"].items():
        profiles = svc.get("profiles", [])
        if "gpu" in profiles and "mock" in profiles:
            # api and caddy run in both. No model service may.
            assert name in ("api", "caddy"), f"{name} is in both profiles"
