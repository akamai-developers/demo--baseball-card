"""The bearer check. This is the part of the demo that must not be wrong."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from common.auth import (
    MIN_KEY_LENGTH,
    assert_key_is_usable,
    auth_headers,
    check_bearer,
    parse_bearer,
)

KEY = "k" * 64


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        "Bearer",
        "Bearer ",
        f"Basic {KEY}",
        KEY,
        f"Token {KEY}",
        f"Bearer{KEY}",
    ],
)
def test_parse_rejects_anything_that_is_not_a_bearer_header(header: str | None) -> None:
    assert parse_bearer(header) is None


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_scheme_is_case_insensitive(scheme: str) -> None:
    assert parse_bearer(f"{scheme} {KEY}") == KEY


def test_extra_whitespace_is_tolerated() -> None:
    assert parse_bearer(f"  Bearer   {KEY}  ") == KEY


def test_right_key_passes() -> None:
    check_bearer(f"Bearer {KEY}", KEY)


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", f"Basic {KEY}", "Bearer wrong", f"Bearer {KEY}x", f"Bearer {KEY[:-1]}"],
)
def test_wrong_or_missing_key_is_401(header: str | None) -> None:
    with pytest.raises(HTTPException) as caught:
        check_bearer(header, KEY)
    assert caught.value.status_code == 401
    assert caught.value.headers["WWW-Authenticate"] == "Bearer"


def test_a_service_with_no_key_denies_everything() -> None:
    """A service that failed to load its key must not become an open service."""
    for header in [None, "Bearer ", "Bearer anything", "Bearer "]:
        with pytest.raises(HTTPException) as caught:
            check_bearer(header, "")
        assert caught.value.status_code == 401


def test_the_401_body_never_names_the_key() -> None:
    with pytest.raises(HTTPException) as caught:
        check_bearer("Bearer wrong", KEY)
    assert KEY not in str(caught.value.detail)
    assert "wrong" not in str(caught.value.detail)


def test_real_mode_refuses_to_start_without_a_key() -> None:
    with pytest.raises(RuntimeError, match="missing"):
        assert_key_is_usable("", "real", "cutout")


def test_real_mode_refuses_to_start_on_a_short_key() -> None:
    short = "a" * (MIN_KEY_LENGTH - 1)
    with pytest.raises(RuntimeError) as caught:
        assert_key_is_usable(short, "real", "art")
    assert short not in str(caught.value)


def test_real_mode_accepts_a_long_enough_key() -> None:
    assert_key_is_usable("a" * MIN_KEY_LENGTH, "real", "api")


def test_mock_mode_warns_but_starts(caplog: pytest.LogCaptureFixture) -> None:
    assert_key_is_usable("short", "mock", "api")
    assert "BACKEND=mock" in caplog.text


def test_auth_headers_shape() -> None:
    assert auth_headers(KEY) == {"Authorization": f"Bearer {KEY}"}
