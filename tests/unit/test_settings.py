"""Settings, and the rule that an empty service URL means the service is not here."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.settings import Settings


def test_defaults_are_the_laptop_case() -> None:
    settings = Settings(_env_file=None)
    assert settings.backend == "mock"
    assert settings.device == "cpu"
    assert settings.service_urls == {}
    assert settings.retention_hours == 24


def test_only_configured_services_are_health_checked() -> None:
    settings = Settings(_env_file=None, cutout_url="http://cutout:8001", llm_url="")
    assert settings.service_urls == {"cutout": "http://cutout:8001"}


def test_trailing_slashes_are_dropped() -> None:
    settings = Settings(_env_file=None, art_url="http://art:8002/")
    assert settings.service_urls["art"] == "http://art:8002"


def test_key_whitespace_is_stripped() -> None:
    settings = Settings(_env_file=None, card_api_key="  abc  ")
    assert settings.card_api_key == "abc"


def test_unknown_env_vars_do_not_break_startup() -> None:
    """The .env on a laptop also carries deploy settings. Services ignore them."""
    settings = Settings(_env_file=None, ALLOW_CLOUD_DEPLOY="1", GPU_COUNT="2")
    assert settings.backend == "mock"


@pytest.mark.parametrize("backend", ["mock", "real"])
def test_is_real(backend: str) -> None:
    assert Settings(_env_file=None, backend=backend).is_real == (backend == "real")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("art_pool_size", -1),
        ("art_pool_size", 13),
        ("comps_count", 11),
        ("render_concurrency", 0),
        ("retention_hours", 0),
        # Caddy refuses a body over 20 MB, so a larger upload limit could
        # never be reached.
        ("max_upload_mb", 20),
    ],
)
def test_a_setting_out_of_its_range_stops_the_service(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_every_setting_in_env_example_parses() -> None:
    """The defaults block of .env.example, as a service reads it."""
    from tests.conftest import REPO_ROOT

    Settings(_env_file=REPO_ROOT / ".env.example")
