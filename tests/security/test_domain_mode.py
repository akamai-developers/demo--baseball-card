"""Tunnel mode and domain mode, checked against the real Caddy and the real compose file.

Tunnel mode is the default and publishes one loopback port. Domain mode
replaces that with 80 and 443 on every interface, and is the only mode where
anything is reachable from the internet.

The Caddy checks run `caddy validate`, not only `caddy adapt`. adapt only
parses. validate provisions the modules too, which is what catches a matcher
that parses and then fails at startup.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
import yaml

from tests.conftest import REPO_ROOT


class ComposeLoader(yaml.SafeLoader):
    """SafeLoader plus compose's merge tags.

    `ports: !override` is how docker-compose.domain.yml replaces the port list
    instead of adding to it. PyYAML refuses an unknown tag, so the tag is
    dropped and the value underneath it is kept.
    """


def _drop_tag(loader: yaml.Loader, suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


ComposeLoader.add_multi_constructor("!", _drop_tag)

CADDY_IMAGE = "caddy:2.10.2-alpine"
DOMAIN = "cards.example.com"
OVERRIDE = "docker-compose.domain.yml"


def test_the_tests_run_the_image_compose_runs() -> None:
    source = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    assert source["services"]["caddy"]["image"] == CADDY_IMAGE


def caddy(docker: list[str], command: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    args = [*docker, "run", "--rm"]
    for name, value in env.items():
        args += ["-e", f"{name}={value}"]
    args += [
        "-v",
        f"{REPO_ROOT / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
        CADDY_IMAGE,
        "caddy",
        command,
        "--config",
        "/etc/caddy/Caddyfile",
    ]
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def servers(docker: list[str], env: dict[str, str]) -> tuple[list[dict], dict]:
    result = caddy(docker, "adapt", env)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    return list(config["apps"]["http"]["servers"].values()), config


# ---- the Caddyfile, in both modes ------------------------------------------


@pytest.mark.parametrize("env", [{}, {"CADDY_SITE": DOMAIN}], ids=["tunnel", "domain"])
def test_the_gateway_config_provisions(docker: list[str], env: dict[str, str]) -> None:
    result = caddy(docker, "validate", env)
    assert result.returncode == 0, f"caddy rejected the config:\n{result.stderr}"
    assert "Valid configuration" in result.stdout + result.stderr


def test_tunnel_mode_serves_plain_http_and_asks_for_no_certificate(docker: list[str]) -> None:
    """A site address with no hostname must not drag in the TLS app.

    If it did, Caddy would try to get a certificate for a name it does not have
    and the gateway would sit there retrying instead of serving.
    """
    found, config = servers(docker, {})
    assert [s["listen"] for s in found] == [[":8443"]]
    assert "tls" not in config["apps"], "tunnel mode pulled in the TLS app"


def test_domain_mode_listens_on_443_for_the_domain_only(docker: list[str]) -> None:
    found, _ = servers(docker, {"CADDY_SITE": DOMAIN})
    assert [s["listen"] for s in found] == [[":443"]]
    # Only requests for the domain are served. A request to the bare IP gets
    # nothing, which is what keeps a scanner from finding the api.
    assert found[0]["routes"][0]["match"] == [{"host": [DOMAIN]}]
    # Off, because the firewall opens TCP only.
    assert found[0]["protocols"] == ["h1", "h2"]


def test_hsts_is_sent_on_tls_and_only_on_tls() -> None:
    """A tunnel is plain HTTP on a loopback port. HSTS there would be a lie."""
    caddyfile = (REPO_ROOT / "Caddyfile").read_text()
    assert "@https protocol https" in caddyfile
    assert "header @https Strict-Transport-Security" in caddyfile
    assert not re.search(r"^\s*header\s+Strict-Transport-Security", caddyfile, re.MULTILINE)


def test_the_site_address_defaults_to_the_tunnel_port() -> None:
    """With no environment at all, the gateway comes up in the safe mode."""
    assert "{$CADDY_SITE::8443}" in (REPO_ROOT / "Caddyfile").read_text()


# ---- the compose overlay ---------------------------------------------------


def domain_config(docker: list[str]) -> dict:
    result = subprocess.run(
        [
            *docker,
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            OVERRIDE,
            "--profile",
            "gpu",
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "DOMAIN": DOMAIN},
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed:\n{result.stderr}")
    return json.loads(result.stdout)


def test_domain_mode_publishes_80_and_443_and_nothing_else(docker: list[str]) -> None:
    config = domain_config(docker)
    publishing = {name for name, svc in config["services"].items() if svc.get("ports")}
    assert publishing == {"caddy"}, f"domain mode publishes ports on {sorted(publishing)}"

    ports = config["services"]["caddy"]["ports"]
    assert sorted(int(p["target"]) for p in ports) == [80, 443]
    # The loopback mapping from docker-compose.yml is gone, not added to.
    assert all(p.get("host_ip") in (None, "", "0.0.0.0") for p in ports), ports


def test_domain_mode_hands_caddy_the_domain(docker: list[str]) -> None:
    config = domain_config(docker)
    assert config["services"]["caddy"]["environment"]["CADDY_SITE"] == DOMAIN


def test_domain_mode_refuses_to_render_without_a_domain(docker: list[str]) -> None:
    """An empty DOMAIN here would open 80 and 443 while Caddy served :8443.

    --env-file /dev/null so the repo's own .env cannot supply a DOMAIN and turn
    this into a test of whoever ran it last. Unset has to mean unset.
    """
    env = dict(os.environ)
    env.pop("DOMAIN", None)
    result = subprocess.run(
        [
            *docker,
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "docker-compose.yml",
            "-f",
            OVERRIDE,
            "config",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 0, "compose rendered domain mode with no DOMAIN set"
    assert "DOMAIN" in result.stderr


def test_the_override_only_touches_the_gateway() -> None:
    """Nothing else may change between the two modes. One file, one job."""
    source = yaml.load((REPO_ROOT / OVERRIDE).read_text(), Loader=ComposeLoader)
    assert set(source["services"]) == {"caddy"}
