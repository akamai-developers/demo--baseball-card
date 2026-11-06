"""make check and make smoke, run against the mock stack.

check.sh's key checks are the same requests against the mock stack as against
a node: three routes, each 401 with no key and with a wrong one, and 200 with
it. Then it sees mock/wordlist and says the GPU checks do not apply. smoke.sh
makes a real card through the stack with cardctl and then fails, because no
model step ran on a GPU. Failing there is its job.

Both read the gateway the way they do on a laptop with a node: localhost on
GATEWAY_PORT, where make tunnel would be. So they skip when .env sets DOMAIN,
which would point them at a node.
"""

from __future__ import annotations

import os
import subprocess
from urllib.parse import urlparse

import pytest

from tests.conftest import REPO_ROOT, dotenv_value, refute_key

pytestmark = pytest.mark.integration


def run(script: str, gateway: str, **env: str) -> subprocess.CompletedProcess:
    if dotenv_value("DOMAIN"):
        pytest.skip("DOMAIN is set in .env, so the script would ask the node")
    return subprocess.run(
        ["bash", str(REPO_ROOT / "deploy" / "scripts" / script)],
        cwd=REPO_ROOT,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "GATEWAY_PORT": str(urlparse(gateway).port),
            **env,
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_check_passes_its_key_checks_on_the_mock_stack(gateway, mock_backends, live_key) -> None:
    done = run("check.sh", gateway)
    assert done.returncode == 0, done.stderr
    for path in ("/v1/models", "/healthz", "/gpu"):
        assert f"GET {path} with no key: 401" in done.stderr
        assert f"GET {path} with a wrong key: 401" in done.stderr
        assert f"GET {path} with the key: 200" in done.stderr
    assert "every service reports its model loaded" in done.stderr
    assert "This was the mock stack, so nothing about GPUs was checked." in done.stderr
    refute_key(done.stdout + done.stderr, live_key, "check.sh's output")


def test_smoke_fails_a_card_the_mock_stack_made(gateway, mock_backends, live_key, tmp_path) -> None:
    done = run("smoke.sh", gateway, OUT=str(tmp_path))
    assert done.returncode != 0
    assert "card-front.png is 1500x2100" in done.stderr
    assert "card-back.png is 1500x2100" in done.stderr
    for step in ("cutout", "art", "text"):
        assert f"{step} says it ran on mock-gpu0. It has to start with akamai-gpu" in done.stderr
    assert "3 check(s) failed" in done.stderr
    refute_key(done.stdout + done.stderr, live_key, "smoke.sh's output")
