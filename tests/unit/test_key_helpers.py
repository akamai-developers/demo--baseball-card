"""The test suite never prints the stack's key.

The key in .env can be a live node's. A failing test's traceback lists its
arguments, and a failing assert spells out both sides. tests/conftest.py gives
the key a repr that hides it, and two helpers that fail without saying what
they compared. This holds them to that.
"""

from __future__ import annotations

import pytest

from tests.conftest import Secret, confirm_key, refute_key

KEY = "k" * 64


def test_the_key_works_as_a_string_and_prints_as_a_name() -> None:
    key = Secret(KEY)
    assert key == KEY
    assert f"Bearer {key}" == f"Bearer {KEY}"
    assert KEY not in repr(key)
    assert repr(key) == "<CARD_API_KEY>"
    assert KEY not in repr([key, {"key": key}])


@pytest.mark.parametrize(
    ("check", "args"),
    [
        (refute_key, (f"a log line with {KEY} in it", KEY, "the log")),
        (confirm_key, (KEY[:-1], KEY, "the header")),
    ],
)
def test_a_failure_says_where_and_not_what(check, args) -> None:
    with pytest.raises(AssertionError) as caught:
        check(*args)
    assert KEY[:8] not in str(caught.value)
    assert args[2] in str(caught.value)


def test_the_helpers_pass_quietly() -> None:
    refute_key("nothing here", KEY, "the log")
    refute_key(None, KEY, "the log")
    confirm_key(KEY, KEY, "the header")
