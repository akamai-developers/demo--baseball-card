"""Nothing of the event build this repo was cut from came over with it.

That build had a high-score page for a big screen, visitor logins, card pages
to pass around, paper copies made by an outside firm and the file drop they
used, a stand that read a partner's folders, two pitching lanes, and the names
of the event, its ballpark and its partners. This repo has none of it.

Every file git would commit is read for the words those parts went by, and a
hit has to be one of the uses in ALLOWED, each with the reason it is not that
part. A file named for one of them is refused outright, and so are the markers
of unfinished work. The words, and the samples that prove the check catches
them, are written in rot13, so the repo does not carry the names. This file is
the one file the check does not read, since it spells out the uses it allows.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, committable, text_of

THIS_FILE = "tests/unit/test_cut_list.py"


def plain(rot13: str) -> str:
    return codecs.decode(rot13, "rot13")


CUT_WORDS = [
    plain(word)
    for word in (
        "obneq",
        "yrnqreobneq",
        "frffvba",
        "funer",
        "cevag",
        "qebcobk",
        "obbgu",
        "qevir",
        "fgngvba",
        "fhzzvg",
        "benpyr",
        "iraqbe",
        "yvirk",
        "xnaqr",
    )
]
CUT = re.compile("|".join(CUT_WORDS), re.IGNORECASE)

# The uses a reviewer accepts: a pattern, the paths it holds for (None for every
# file), and why it is not the part that was cut.
ALLOWED: list[tuple[str, tuple[str, ...] | None, str]] = [
    (r"\bsessionStorage\b", None, "the tab's storage the page keeps the key in"),
    (r'scope="session"', None, "a pytest fixture that lives for the whole run"),
    (r"\bnew_cdp_session\b", None, "Playwright's channel to Chromium's DevTools"),
    (r"\bprint\(", None, "Python's print()"),
    (r"\bprintf\b|\{ print \$|--no-print-directory", None, "the shell's printf, awk, make"),
    (r"(?<![A-Za-z])[Pp]rint(?:s|ed|ing)(?![A-Za-z])", None, "the verb: what a command writes"),
    (
        r"(?<![A-Za-z])[Ss]har(?:e|es|ed|ing)(?![A-Za-z])(?!\s+(?:pages?|links?|urls?|buttons?)\b)",
        None,
        "the verb: three models share a card",
    ),
    (r"(?i)drivers?", None, "the NVIDIA driver, and the network driver in compose"),
    (r"(?i)\bline[\s-]+drives?\b", None, "a ball hit hard and flat, in the text model's rules"),
    (r"\bdriven\b", None, "healthcheck-driven"),
    (r"\bKeyboardInterrupt\b", None, "ctrl-c in cardctl"),
    (r"(?<=[/._\-\"`])vendor|vendor(?=[/._\-\"`])", None, "web/vendor.lock: the face detector"),
    (
        r"(?i)\bvendor\b",
        ("web/vendor.py", "tests/unit/test_web_vendor.py"),
        "the fetcher for web/vendor.lock, and its test, by the fetcher's own name",
    ),
    (
        r"(?i)[\w./-]*board[\w./-]*",
        ("packages/comps/", "tests/unit/test_comps_"),
        "Baseball Savant's ranking pages, where the Statcast table comes from",
    ),
]

# The face detector's files, which the prompt names: the lock, the fetcher
# beside it, and the fetcher's test.
NAMED_FOR_A_CUT_WORD = ["tests/unit/test_web_vendor.py", "web/vendor.lock", "web/vendor.py"]

MARKERS = re.compile("|".join(plain(word) for word in (r"\oGBQB\o", r"\oSVKZR\o", "yrtnpl")), re.I)
# The Linode provider's own value for interfaces on the configuration profile.
PROVIDER_VALUE = re.compile('"' + plain("yrtnpl_pbasvt") + '"')


def name(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def blank(text: str, pattern: re.Pattern) -> str:
    """The text with every match of pattern turned to spaces, its lines kept."""
    return pattern.sub(lambda found: re.sub(r"[^\n]", " ", found.group()), text)


def line_of(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def unexplained(text: str, path: str = "") -> list[tuple[int, str]]:
    """Each cut word left once the allowed uses are blanked, as (line, word)."""
    for pattern, paths, _why in ALLOWED:
        if paths is None or path.startswith(paths):
            text = blank(text, re.compile(pattern))
    return [(line_of(text, hit.start()), hit.group()) for hit in CUT.finditer(text)]


def texts() -> list[tuple[str, str]]:
    """Every committable text file but this one, by its path in the repo."""
    found = [(name(path), text_of(path)) for path in committable()]
    return [(path, text) for path, text in found if text is not None and path != THIS_FILE]


def test_no_file_holds_a_cut_word_it_cannot_explain() -> None:
    found = [
        f"{path}:{line}: {word}" for path, text in texts() for line, word in unexplained(text, path)
    ]
    assert found == []


def test_no_file_is_named_for_a_part_that_was_cut() -> None:
    named = [name(path) for path in committable() if CUT.search(name(path))]
    assert named == NAMED_FOR_A_CUT_WORD


def test_no_marker_of_unfinished_work() -> None:
    found = [
        f"{path}:{line_of(text, hit.start())}: {hit.group()}"
        for path, text in texts()
        for hit in MARKERS.finditer(blank(text, PROVIDER_VALUE))
    ]
    assert found == []


@pytest.mark.parametrize(
    "rot13",
    [
        "gur yrnqreobneq",
        "obneq.ugzy",
        "FUNER_HEY=uggcf://rknzcyr.pbz",
        "n funer cntr sbe rirel pneq",
        "CEVAG_QVE=/fei/bhg",
        "gur cevag furrg",
        "/pneqf/nop/cevag.wct",
        "Tbbtyr Qevir",
        "FGNGVBA=2",
        "n frffvbaf gnoyr",
        "gur iraqbe'f sbyqre",
        "gur obbgu",
        "n qebcobk sbyqre",
        "fhzzvg",
        "benpyr",
        "yvirk",
        "xnaqr",
    ],
)
def test_the_check_catches_the_parts_that_were_cut(rot13: str) -> None:
    """So the check is not vacuous."""
    assert unexplained(plain(rot13)), plain(rot13)


@pytest.mark.parametrize(
    "sample",
    [
        "key = sessionStorage.getItem(KEY_NAME)",
        '@pytest.fixture(scope="session")',
        'print(f"card {card_id}")',
        "printf '%s\\n' \"$line\"",
        "make help prints every target",
        "the three models share one card, and shared_metrics",
        "the NVIDIA driver 580, nvidia_driver_branch",
        plain("10 gb 25 vf n yvar\n  qevir"),
        "make web-vendor fills web/vendor/ from web/vendor.lock",
        "except KeyboardInterrupt:",
    ],
)
def test_the_uses_a_reviewer_accepts_pass(sample: str) -> None:
    assert unexplained(sample) == []


def test_a_ranking_page_is_only_fine_where_the_statcast_table_is_fetched() -> None:
    url = plain("uggcf://onfronyyfninag.zyo.pbz/yrnqreobneq/ong-genpxvat")
    assert unexplained(url, "packages/comps/src/comps/fetch_statcast.py") == []
    assert unexplained(url, "web/app/app.js") != []


def test_a_marker_is_caught_and_the_providers_value_is_not() -> None:
    assert MARKERS.search(plain("# GBQB: yngre"))
    assert MARKERS.search(plain("n yrtnpl oenapu"))
    assert not MARKERS.search(
        blank(plain('vagresnpr_trarengvba = "yrtnpl_pbasvt"'), PROVIDER_VALUE)
    )
