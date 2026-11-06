"""The writing rules, as data.

CONTRIBUTING.md bans the em-dash and a list of words from everything a person
reads: the card, the docs, the prompt. `tests/unit/test_copy.py` holds those
files to it. The list lives here, next to the text step, so anything the api
checks before it goes on a card uses the same words the test does. It cannot
live in `text.py`: the test reads that file, and the list would fail it.

Matching is case-insensitive and by whole word. "unlock" is caught, "unlocked"
is not, because that is how the list is written. A phrase broken across a line
break still counts, and a curly apostrophe counts as a straight one.

`api.text.check` holds the card back to this list and to one of its own, with
`phrases_in`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache

EM_DASH = "—"

BANNED = (
    "delve",
    "delves",
    "delving",
    "dive into",
    "dives into",
    "diving into",
    "leverage",
    "leverages",
    "leveraging",
    "robust",
    "robustly",
    "seamless",
    "seamlessly",
    "holistic",
    "unlock",
    "unlocks",
    "unlocking",
    "supercharge",
    "supercharges",
    "elevate",
    "elevates",
    "elevating",
    "empower",
    "empowers",
    "empowering",
    "game-changer",
    "game changer",
    "game-changing",
    "cutting-edge",
    "state-of-the-art",
    "effortless",
    "effortlessly",
    "unleash",
    "showcase",
    "showcases",
    "showcasing",
    "testament",
    "it's not just",
    "isn't just",
    "it is not just",
    "here's the thing",
    "not just a",
)


@cache
def _pattern(phrase: str) -> re.Pattern[str]:
    words = [re.escape(word).replace("'", "['’]") for word in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(words) + r"\b", re.IGNORECASE)


def violations(text: str) -> list[tuple[int, str]]:
    """Every rule `text` breaks, as (line number, what), in reading order."""
    found = [(match.start(), "em-dash") for match in re.finditer(EM_DASH, text)]
    for phrase in BANNED:
        found.extend((match.start(), phrase) for match in _pattern(phrase).finditer(text))
    return [(text.count("\n", 0, start) + 1, what) for start, what in sorted(found)]


def phrases_in(text: str, phrases: Iterable[str] = BANNED) -> list[str]:
    """Which of `phrases` are in `text`, matched the same way, in the list's order."""
    return [phrase for phrase in phrases if _pattern(phrase).search(text)]


__all__ = ["BANNED", "EM_DASH", "phrases_in", "violations"]
