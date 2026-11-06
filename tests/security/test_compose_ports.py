"""Caddy is the only service that publishes a port, and without the domain
overlay that port is bound to the loopback address. This reads the rendered
compose file, not the source, so an override cannot quietly open something.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from tests.conftest import REPO_ROOT, compose_config, dotenv_value, refute_key

GATEWAY_SERVICE = "caddy"


def rendered(docker: list[str], profile: str, env: dict[str, str] | None = None) -> dict:
    return compose_config(docker, (profile,), env)


@pytest.mark.parametrize("profile", ["mock", "gpu"])
def test_only_the_gateway_publishes_a_port(docker: list[str], profile: str) -> None:
    config = rendered(docker, profile)
    publishing = {name for name, svc in config["services"].items() if svc.get("ports")}
    assert publishing == {GATEWAY_SERVICE}, (
        f"in the {profile} profile these services publish host ports: {sorted(publishing)}"
    )


@pytest.mark.parametrize("profile", ["mock", "gpu"])
@pytest.mark.parametrize("domain", ["", "cards.example.com"])
def test_the_gateway_binds_loopback_without_the_domain_overlay(
    docker: list[str], profile: str, domain: str
) -> None:
    """DOMAIN alone opens nothing. Only docker-compose.domain.yml does, and
    tests/security/test_domain_mode.py holds that file."""
    config = rendered(docker, profile, env={"DOMAIN": domain})
    ports = config["services"][GATEWAY_SERVICE]["ports"]
    assert len(ports) == 1, "the gateway publishes one port"
    assert ports[0].get("host_ip") == "127.0.0.1", "the gateway is reachable off the box"
    assert int(ports[0]["target"]) == 8443


def test_the_gateway_port_follows_gateway_port(docker: list[str]) -> None:
    config = rendered(docker, "mock", env={"GATEWAY_PORT": "8446"})
    assert config["services"][GATEWAY_SERVICE]["ports"][0]["published"] == "8446"


@pytest.mark.parametrize("profile", ["mock", "gpu"])
def test_the_gateway_never_holds_the_key(docker: list[str], profile: str) -> None:
    """Caddy routes by path and checks nothing, so it is not handed .env.

    The rendered config folds an env_file into `environment`, so this sees both.
    It compares names and never prints a value: with the bug this looks for,
    the values would hold this box's key.
    """
    config = rendered(docker, profile)
    caddy = config["services"][GATEWAY_SERVICE]
    names = sorted(caddy.get("environment") or {})
    assert "CARD_API_KEY" not in names, "Caddy is handed the key"
    refute_key(json.dumps(caddy), dotenv_value("CARD_API_KEY") or "", "Caddy's config")


@pytest.mark.parametrize("profile", ["mock", "gpu"])
def test_no_service_runs_in_host_network_mode(docker: list[str], profile: str) -> None:
    config = rendered(docker, profile)
    for name, svc in config["services"].items():
        assert svc.get("network_mode") != "host", f"{name} is on the host network"


# A value that is nothing but a ${VAR} reference carries no secret.
INTERPOLATION = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::[-?][^}]*)?\}$")

SECRET_NAMES = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def test_no_secret_is_baked_into_the_compose_file() -> None:
    source = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    for name, svc in source["services"].items():
        for key, value in (svc.get("environment") or {}).items():
            if not any(word in key.upper() for word in SECRET_NAMES):
                continue
            assert value is None or INTERPOLATION.fullmatch(str(value)), (
                f"{name} sets {key} to a literal in docker-compose.yml. "
                "Secrets belong in .env, and only a ${VAR} reference may name one here."
            )
