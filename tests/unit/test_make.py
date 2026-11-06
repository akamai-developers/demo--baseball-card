"""`make help` and `make env`.

help prints every target once, under the Makefile's own `# ---- ` section
lines, with a blank line between groups. env makes .env from .env.example with
a fresh key, and keeps the key it finds.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="no make on this box")


def make(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")},
    )


def test_make_help_prints_every_target_once_in_its_group() -> None:
    shown = make("help")
    assert shown.returncode == 0, shown.stderr
    text = re.sub(r"\x1b\[[0-9;]*m", "", shown.stdout)
    groups = [block.splitlines() for block in text.strip("\n").split("\n\n")]

    makefile = (REPO_ROOT / "Makefile").read_text()
    sections = re.findall(r"^# ---- (.+?) -+$", makefile, re.M)
    assert [lines[0] for lines in groups] == sections

    listed = [line.split()[0] for lines in groups for line in lines[1:]]
    phony = makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0].replace("\\", " ").split()
    assert sorted(listed) == sorted(phony)
    assert len(listed) == len(set(listed)), "a target is listed twice"


def key_lines(dotenv: Path) -> list[str]:
    return [line for line in dotenv.read_text().splitlines() if line.startswith("CARD_API_KEY=")]


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A folder with the Makefile and .env.example, and no .env."""
    for name in ("Makefile", ".env.example"):
        shutil.copy2(REPO_ROOT / name, tmp_path / name)
    return tmp_path


def test_env_makes_a_key_in_place_at_mode_600(scratch: Path) -> None:
    made = make("env", cwd=scratch)
    assert made.returncode == 0, made.stderr
    dotenv = scratch / ".env"
    lines = key_lines(dotenv)
    assert len(lines) == 1
    assert re.fullmatch(r"CARD_API_KEY=[0-9a-f]{64}", lines[0])
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    # Where .env.example has it, under its comment, not appended at the end.
    example = (scratch / ".env.example").read_text().splitlines()
    written = dotenv.read_text().splitlines()
    assert written.index(lines[0]) == example.index("CARD_API_KEY=")
    assert len(written) == len(example)
    # The key is never printed.
    assert lines[0].split("=", 1)[1] not in made.stdout + made.stderr
    assert not (scratch / ".env.tmp").exists()


def test_env_keeps_a_key_it_finds(scratch: Path) -> None:
    make("env", cwd=scratch)
    first = key_lines(scratch / ".env")
    again = make("env", cwd=scratch)
    assert again.returncode == 0
    assert key_lines(scratch / ".env") == first
    assert "generated" not in again.stdout


def test_env_replaces_a_key_too_short_to_use(scratch: Path) -> None:
    (scratch / ".env").write_text("GATEWAY_PORT=8443\nCARD_API_KEY=short\n")
    make("env", cwd=scratch)
    lines = key_lines(scratch / ".env")
    assert len(lines) == 1 and len(lines[0]) == len("CARD_API_KEY=") + 64
    assert (scratch / ".env").read_text().startswith("GATEWAY_PORT=8443\n")
