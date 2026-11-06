"""deploy/models.env pins the three model repos. Two other places hold the same
pins, because that is where they are used: the cutout and art services as
Python constants, and docker-compose.yml as the defaults of vLLM's command.

Three copies of a 40-character commit hash is a bad idea unless something checks
they match, so that is what this file is for. Get it wrong and the node spends
twenty minutes downloading weights no service will load.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.conftest import REPO_ROOT, compose_config, load_script

MODELS_ENV = REPO_ROOT / "deploy" / "models.env"
COMPOSE = REPO_ROOT / "docker-compose.yml"


def pins() -> dict[str, str]:
    """deploy/models.env as a dict. It is three groups of KEY=value and nothing else."""
    values = {}
    for line in MODELS_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        values[name] = value
    return values


def test_the_file_is_only_key_equals_value() -> None:
    """GET /models reads it as KEY=value lines, and a shell can source it.
    Nothing else belongs in it."""
    for number, line in enumerate(MODELS_ENV.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s;|&$`()]*", stripped), (
            f"deploy/models.env line {number} is not a plain KEY=value: {line!r}"
        )


def test_the_cutout_pin_matches_the_cutout_service() -> None:
    from cutout.main import REAL_LICENSE, REAL_MODEL, REAL_REVISION

    values = pins()
    assert values["CUTOUT_MODEL"] == REAL_MODEL
    assert values["CUTOUT_REVISION"] == REAL_REVISION
    assert values["CUTOUT_LICENSE"] == REAL_LICENSE


def test_the_art_pin_matches_the_art_service() -> None:
    from art.main import REAL_LICENSE, REAL_MODEL, REAL_REVISION

    values = pins()
    assert values["ART_MODEL"] == REAL_MODEL
    assert values["ART_REVISION"] == REAL_REVISION
    assert values["ART_LICENSE"] == REAL_LICENSE


def test_the_llm_pin_matches_what_vllm_is_told_to_serve() -> None:
    """The api sends this name in every request and vLLM 404s a name it does not serve.

    docker-compose.yml carries it as the default of ${LLM_MODEL}, and the node's
    .env carries the models.env value. If the two drift, vLLM serves one name
    and the api asks for the other.
    """
    values = pins()
    compose = yaml.safe_load(COMPOSE.read_text())
    defaults = {}
    for entry in compose["services"]["llm-gpu"]["command"]:
        match = re.fullmatch(r"\$\{([A-Z_]+):-(.*)\}", str(entry))
        if match:
            defaults[match.group(1)] = match.group(2)

    assert defaults["LLM_MODEL"] == values["LLM_MODEL"]
    assert defaults["LLM_REVISION"] == values["LLM_REVISION"]


def test_every_revision_is_a_full_commit_hash() -> None:
    """A branch name would let the weights change under a pinned deploy.

    BiRefNet is the one that matters most: it is loaded with
    trust_remote_code=True, so its revision decides which Python runs.
    """
    values = pins()
    for name in ("CUTOUT_REVISION", "ART_REVISION", "LLM_REVISION"):
        assert re.fullmatch(r"[0-9a-f]{40}", values[name]), (
            f"{name} is {values[name]!r}, which is not a 40-character commit hash"
        )


# ---- what the node downloads ----------------------------------------------------

# FLUX.2 [klein] 4B at the pinned commit, as the Hub listed it on 2026-11-06:
# every file and its size in bytes, rounded to the MB.
FLUX_REPO = {
    ".gitattributes": 0,
    "LICENSE.md": 0,
    "README.md": 0,
    "editing.jpg": 2_500_000,
    "flux-2-klein-4b.safetensors": 7_751_100_000,
    "model_index.json": 0,
    "others.jpg": 3_400_000,
    "realism.jpg": 2_900_000,
    "scheduler/scheduler_config.json": 0,
    "text_encoder/config.json": 0,
    "text_encoder/generation_config.json": 0,
    "text_encoder/model-00001-of-00002.safetensors": 4_967_200_000,
    "text_encoder/model-00002-of-00002.safetensors": 3_077_800_000,
    "text_encoder/model.safetensors.index.json": 0,
    "tokenizer/added_tokens.json": 0,
    "tokenizer/chat_template.jinja": 0,
    "tokenizer/merges.txt": 1_700_000,
    "tokenizer/special_tokens_map.json": 0,
    "tokenizer/tokenizer.json": 11_400_000,
    "tokenizer/tokenizer_config.json": 0,
    "tokenizer/vocab.json": 2_800_000,
    "transformer/config.json": 0,
    "transformer/diffusion_pytorch_model.safetensors": 7_751_100_000,
    "vae/config.json": 0,
    "vae/diffusion_pytorch_model.safetensors": 168_100_000,
}


@pytest.fixture
def pull_models(monkeypatch):
    """deploy/scripts/pull_models.py with huggingface_hub stood in for, writing
    down each download it is asked for."""
    import sys
    import types

    asked = []
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = lambda **kwargs: (
        asked.append(kwargs) or f"/hf-cache/{kwargs['repo_id']}"
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    module = load_script(REPO_ROOT / "deploy" / "scripts" / "pull_models.py")
    module.asked = asked
    return module


def test_the_node_downloads_each_pin_at_its_commit(pull_models, capsys) -> None:
    assert pull_models.main(["pull_models.py", str(MODELS_ENV)]) == 0
    values = pins()
    assert [(a["repo_id"], a["revision"]) for a in pull_models.asked] == [
        (values["CUTOUT_MODEL"], values["CUTOUT_REVISION"]),
        (values["ART_MODEL"], values["ART_REVISION"]),
        (values["LLM_MODEL"], values["LLM_REVISION"]),
    ]
    # BiRefNet and Gemma whole, FLUX by its folders.
    assert [a["allow_patterns"] is None for a in pull_models.asked] == [True, False, True]
    assert "==> FLUX.2 klein: black-forest-labs/FLUX.2-klein-4B at" in capsys.readouterr().out


def test_flux_comes_down_by_the_folders_diffusers_reads(pull_models) -> None:
    """Not the second copy of the transformer at the root, 7.75 GB, nor the
    sample pictures. 16.0 GB of the repo's 23.7."""
    import fnmatch

    # huggingface_hub matches allow_patterns with fnmatch, whose * crosses a /.
    kept = {
        name
        for name in FLUX_REPO
        if any(fnmatch.fnmatch(name, pattern) for pattern in pull_models.FLUX_FILES)
    }
    folders = {name.split("/")[0] for name in kept if "/" in name}
    assert folders == {"scheduler", "text_encoder", "tokenizer", "transformer", "vae"}
    assert "model_index.json" in kept
    assert "flux-2-klein-4b.safetensors" not in kept
    assert not [name for name in kept if name.endswith(".jpg")]
    assert round(sum(FLUX_REPO[name] for name in kept) / 1e9, 1) == 16.0
    assert round(sum(FLUX_REPO.values()) / 1e9, 1) == 23.7


def test_a_missing_pin_stops_the_download(pull_models, tmp_path, capsys) -> None:
    broken = tmp_path / "models.env"
    broken.write_text(MODELS_ENV.read_text().replace("ART_REVISION=", "# ART_REVISION="))
    assert pull_models.main(["pull_models.py", str(broken)]) == 1
    assert "has no ART_MODEL and ART_REVISION" in capsys.readouterr().err
    assert len(pull_models.asked) == 1


def test_the_download_runs_in_vllms_container_with_its_cache(docker: list[str]) -> None:
    """pull-models.sh borrows the llm-gpu service: its image, its hf-cache
    volume at /hf-cache, HF_HOME there, and HF_TOKEN from compose. So the
    weights land where all three services read them."""
    script = (REPO_ROOT / "deploy" / "scripts" / "pull-models.sh").read_text()
    assert 'run() { docker compose run --rm --no-deps "$@"; }' in script
    assert 'run --entrypoint python3 -v "$REPO_ROOT/deploy:/deploy:ro" llm-gpu' in script
    assert "run --entrypoint chmod llm-gpu -R a+rwX /hf-cache" in script
    llm = compose_config(docker, ("gpu",))["services"]["llm-gpu"]
    assert llm["environment"]["HF_HOME"] == "/hf-cache"
    assert {(v["source"], v["target"]) for v in llm["volumes"]} == {("hf-cache", "/hf-cache")}
    for name in ("cutout-gpu", "art-gpu"):
        svc = compose_config(docker, ("gpu",))["services"][name]
        assert ("hf-cache", "/hf-cache") in {(v["source"], v["target"]) for v in svc["volumes"]}
