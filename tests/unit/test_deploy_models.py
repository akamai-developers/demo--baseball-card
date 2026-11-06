"""deploy/models.env pins the three model repos. Two other places hold the same
pins, because that is where they are used: the cutout and art services as
Python constants, and docker-compose.yml as the defaults of vLLM's command.

Three copies of a 40-character commit hash is a bad idea unless something checks
they match, so that is what this file is for. Get it wrong and the node spends
twenty minutes downloading weights no service will load.
"""

from __future__ import annotations

import re

import yaml

from tests.conftest import REPO_ROOT

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
