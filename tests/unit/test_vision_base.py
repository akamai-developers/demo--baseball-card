"""The torch the real images run, and the lock everything else comes from.

services/vision-base.Dockerfile installs torch and torchvision from the CUDA
wheel index, and the cutout and art images install the rest of their extra
from uv.lock on top. The lock was resolved against one torch, so the base must
install that torch: a different one could miss what timm, kornia or accelerate
were resolved against, and nothing would say so until a model failed to load on
the node.
"""

from __future__ import annotations

import re
import tomllib

import pytest

from tests.conftest import REPO_ROOT

BASE = REPO_ROOT / "services" / "vision-base.Dockerfile"
REAL = {
    "cutout": REPO_ROOT / "services" / "cutout" / "Dockerfile.real",
    "art": REPO_ROOT / "services" / "art" / "Dockerfile.real",
}


def arg(name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", BASE.read_text(), re.MULTILINE)
    assert match, f"vision-base.Dockerfile sets no {name}"
    return match.group(1)


def locked(name: str) -> set[str]:
    """Every version of a package in uv.lock, the local part dropped: 2.14.0+cpu is 2.14.0."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    return {p["version"].split("+")[0] for p in lock["package"] if p["name"] == name}


@pytest.mark.parametrize(
    ("package", "name"), [("torch", "TORCH_VERSION"), ("torchvision", "TORCHVISION_VERSION")]
)
def test_the_base_installs_the_torch_the_lock_holds(package: str, name: str) -> None:
    assert locked(package) == {arg(name)}


def test_the_gpu_base_takes_torch_from_the_cuda_index() -> None:
    """The lock's torch is the CPU build, 2.5 GB smaller, and it cannot see a GPU."""
    assert arg("TORCH_INDEX") == "https://download.pytorch.org/whl/cu126"
    assert arg("BASE_IMAGE").startswith("nvidia/cuda:12.6.")


@pytest.mark.parametrize("service", sorted(REAL))
def test_a_real_image_installs_from_the_lock_and_keeps_the_bases_torch(service: str) -> None:
    """--frozen: the lock's versions or a failed build, never a new resolution.
    --no-install-package torch: the base's CUDA build stays. --inexact: so do
    the CUDA libraries it brought, which the lock does not list."""
    # One logical line per instruction: the continuations joined.
    text = REAL[service].read_text().replace("\\\n", " ")
    sync = re.search(r"^RUN uv sync (.*)$", text, re.MULTILINE)
    assert sync, f"{service}'s Dockerfile.real does not run uv sync"
    command = sync.group(1).split()
    for flag in ("--frozen", "--no-dev", "--inexact", "--extra"):
        assert flag in command, f"{service} runs uv sync without {flag}"
    assert command[command.index("--package") + 1] == f"card-{service}"
    assert command[command.index("--extra") + 1] == "real"
    skipped = {command[i + 1] for i, part in enumerate(command) if part == "--no-install-package"}
    assert "torch" in skipped


@pytest.mark.parametrize("service", sorted(REAL))
def test_a_real_image_is_built_on_the_base(service: str) -> None:
    text = REAL[service].read_text()
    assert "ARG VISION_BASE=baseball-card-vision-base:local" in text
    assert "FROM ${VISION_BASE}" in text
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "VISION_BASE := baseball-card-vision-base:local" in makefile


def test_the_two_real_extras_are_never_locked_together() -> None:
    """BiRefNet's remote code is transformers 4 code. diffusers 0.40 needs 5."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    majors = {p["version"].split(".")[0] for p in lock["package"] if p["name"] == "transformers"}
    assert majors == {"4", "5"}
