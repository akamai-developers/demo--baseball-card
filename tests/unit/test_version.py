"""One version for the workspace: the newest entry in CHANGELOG.md.

`common.__version__` is the only one in the code, and every service reports it.
Every pyproject.toml carries it too. A phase that adds a CHANGELOG entry bumps
them all, or this fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, TEST_KEY

PROJECTS = sorted(
    [
        REPO_ROOT / "pyproject.toml",
        *REPO_ROOT.glob("cli/pyproject.toml"),
        *REPO_ROOT.glob("packages/*/pyproject.toml"),
        *REPO_ROOT.glob("services/*/pyproject.toml"),
    ]
)


def newest() -> str:
    """The version of the first entry, ## [0.1.0] - 2026-11-06."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    return re.search(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", text, re.M).group(1)


def name(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_the_code_carries_the_changelogs_newest_version() -> None:
    from common import __version__

    assert __version__ == newest()


def test_no_other_version_is_written_in_the_code() -> None:
    """One __version__, so the services cannot drift apart."""
    sources = [
        *REPO_ROOT.glob("cli/src/**/*.py"),
        *REPO_ROOT.glob("packages/*/src/**/*.py"),
        *REPO_ROOT.glob("services/*/src/**/*.py"),
    ]
    found = [
        name(path)
        for path in sorted(sources)
        if re.search(r"^__version__ = ", path.read_text(), re.M)
    ]
    assert found == ["packages/common/src/common/__init__.py"]


def test_the_api_reports_it(clean_settings) -> None:
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        health = client.get("/healthz", headers={"Authorization": f"Bearer {TEST_KEY}"})
    assert health.json()["version"] == newest()


def test_every_package_in_the_workspace_is_listed() -> None:
    """The root and every workspace member uv knows about, so a new package
    cannot slip past the next test."""
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    members = root["tool"]["uv"]["workspace"]["members"]
    found = {
        path.parent for pattern in members for path in REPO_ROOT.glob(f"{pattern}/pyproject.toml")
    }
    assert {path.parent for path in PROJECTS} == {REPO_ROOT, *found}


@pytest.mark.parametrize("path", PROJECTS, ids=name)
def test_every_pyproject_carries_it(path: Path) -> None:
    assert tomllib.loads(path.read_text())["project"]["version"] == newest()
