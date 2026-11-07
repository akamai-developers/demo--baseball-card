"""What runs for a card, for GET /models.

The three models the node serves, in pipeline order. The repo, the pinned
commit and the licence of each come from deploy/models.env, the file the deploy
downloads the weights from. What is written here is each model's name and what
it does for the card.

Where each model runs right now is not in here. That changes while the node
runs, and /healthz reports it under services[<service>]: ran_on, loaded,
vram_mb, and the model the service is serving.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from common.settings import Settings

# SPDX ids, as models.env writes them, to the names people read.
LICENSE_NAMES = {"Apache-2.0": "Apache 2.0", "MIT": "MIT"}


class CatalogError(RuntimeError):
    """deploy/models.env is missing, or lacks a pin."""


class ModelInfo(BaseModel):
    # The pipeline step it runs, as the ledger names it.
    step: str
    name: str
    provider: str
    # The Hugging Face repo, the commit the node loads, and that commit's page.
    repo: str
    revision: str
    url: str
    license: str
    # The /healthz entry that says where it runs and whether it is loaded.
    service: str
    # One line on what it does for the card.
    role: str


class Catalog(BaseModel):
    """GET /models."""

    models: list[ModelInfo]


def read_pins(path: Path) -> dict[str, str]:
    """deploy/models.env as a dict. It is KEY=value lines and comments, nothing else."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"cannot read {path}: {exc.strerror}") from exc
    pins = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if sep:
            pins[name.strip()] = value.strip()
    return pins


def pin(pins: dict[str, str], name: str) -> str:
    try:
        return pins[name]
    except KeyError as exc:
        raise CatalogError(f"deploy/models.env has no {name}") from exc


def model(
    pins: dict[str, str], prefix: str, *, step: str, name: str, provider: str, role: str
) -> ModelInfo:
    """One entry, from the three lines models.env holds for it."""
    repo, revision = pin(pins, f"{prefix}_MODEL"), pin(pins, f"{prefix}_REVISION")
    spdx = pin(pins, f"{prefix}_LICENSE")
    return ModelInfo(
        step=step,
        name=name,
        provider=provider,
        repo=repo,
        revision=revision,
        url=f"https://huggingface.co/{repo}/tree/{revision}",
        license=LICENSE_NAMES.get(spdx, spdx),
        service=prefix.lower(),
        role=role,
    )


def build(settings: Settings) -> Catalog:
    """The three entries, read from deploy/models.env."""
    pins = read_pins(settings.models_env)
    return Catalog(
        models=[
            model(
                pins,
                "CUTOUT",
                step="cutout",
                name="BiRefNet",
                provider="ZhengPeng7",
                role="Cuts the person out of the photo.",
            ),
            model(
                pins,
                "ART",
                step="art",
                name="FLUX.2 [klein] 4B",
                provider="Black Forest Labs",
                role="Paints the stadium behind the person.",
            ),
            model(
                pins,
                "LLM",
                step="text",
                name="Gemma 4 12B IT",
                provider="Google",
                role="Writes the nickname, the tagline and the scouting report.",
            ),
        ]
    )
