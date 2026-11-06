"""Every word a person reads, held to the writing rules.

CONTRIBUTING.md bans the em-dash and a list of words from the card templates,
the text model's prompt and fallback, the mock model's word lists, the words
the api sends back to a person, the browser app, the README, the docs, the
examples and the changelog. This reads each of those files whole, comments included, and names
the file, the line and the phrase. Of the example scripts it reads the comments
only: the rest is code.

It cannot tell a flat sentence from a good one. That part is done by ear.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

from api.text import PRAISE
from api.writing import BANNED, EM_DASH, phrases_in, violations
from tests.conftest import REPO_ROOT

CARD = REPO_ROOT / "packages" / "card" / "src" / "card"
TEMPLATES = CARD / "templates"
API_SRC = REPO_ROOT / "services" / "api" / "src" / "api"
TEXT_PY = API_SRC / "text.py"
MOCK_LLM = REPO_ROOT / "services" / "llm-mock" / "src" / "llm_mock" / "main.py"
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_SCRIPTS = sorted(EXAMPLES.glob("*.py"))
WEB_APP = REPO_ROOT / "web" / "app"

FILES = [
    *sorted(TEMPLATES.glob("*.html")),
    # The words the back prints beside its templates: the headings, the made
    # line, the positions.
    CARD / "render.py",
    CARD / "brand" / "README.md",
    TEXT_PY,
    # What GET /models says each model does.
    API_SRC / "catalog.py",
    # The sentences a bad upload gets back.
    API_SRC / "routes.py",
    API_SRC / "photo.py",
    MOCK_LLM,
    REPO_ROOT / "cli" / "src" / "cardctl" / "main.py",
    # The page: its words, the sentences it shows, and its comments.
    WEB_APP / "index.html",
    WEB_APP / "app.css",
    *sorted(WEB_APP.glob("*.js")),
    REPO_ROOT / "web" / "vendor.lock",
    REPO_ROOT / "README.md",
    REPO_ROOT / "CHANGELOG.md",
    REPO_ROOT / "CONTRIBUTING.md",
    *sorted((REPO_ROOT / "docs").rglob("*.md")),
    *sorted(EXAMPLES.glob("*.md")),
]


def name(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def comments(source: str) -> str:
    """The comments of a Python file and nothing else, each on the line it was on
    and without its #, so a phrase that runs from one comment line into the
    next is still one phrase."""
    lines = [""] * (source.count("\n") + 1)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            lines[token.start[0] - 1] = token.string.lstrip("#")
    return "\n".join(lines)


@pytest.mark.parametrize("path", FILES, ids=name)
def test_the_file_keeps_the_writing_rules(path: Path) -> None:
    found = violations(path.read_text(encoding="utf-8"))
    assert not found, "\n".join(f"{name(path)}:{line}: {what}" for line, what in found)


@pytest.mark.parametrize("path", EXAMPLE_SCRIPTS, ids=name)
def test_the_comments_in_an_example_keep_the_writing_rules(path: Path) -> None:
    found = violations(comments(path.read_text(encoding="utf-8")))
    assert not found, "\n".join(f"{name(path)}:{line}: {what}" for line, what in found)


def test_only_the_comments_of_a_script_are_read() -> None:
    """A name in the code is not a word on the page. A comment is, and one that
    runs on to the next line is read as one sentence."""
    source = 'leverage = "robust"\nx = 1  # we will dive\n# into the numbers\n'
    assert violations(comments(source)) == [(2, "dive into")]


def test_no_file_the_rules_name_has_dropped_out() -> None:
    """A renamed file would leave the check without a word. This says so."""
    names = {name(path) for path in FILES}
    for expected in (
        "packages/card/src/card/templates/back.html",
        "packages/card/src/card/templates/front-akamai.html",
        "packages/card/src/card/templates/front-classic.html",
        "packages/card/src/card/render.py",
        "services/api/src/api/text.py",
        "services/api/src/api/catalog.py",
        "services/api/src/api/routes.py",
        "services/llm-mock/src/llm_mock/main.py",
        "web/app/index.html",
        "web/app/app.js",
        "web/app/results.js",
        "web/app/how.js",
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "docs/SECURITY.md",
        "examples/curl.md",
    ):
        assert expected in names, expected
    assert all(path.is_file() for path in FILES)


def test_every_banned_phrase_is_caught_in_any_case() -> None:
    """The check is not vacuous: each phrase is found mid-sentence, shouted."""
    missed = [
        phrase
        for phrase in BANNED
        if violations(f"First line.\nThey {phrase.upper()} it.") != [(2, phrase)]
    ]
    assert not missed, missed


def test_an_em_dash_is_caught_and_a_hyphen_is_not() -> None:
    assert violations(f"one\ntwo {EM_DASH} three") == [(2, "em-dash")]
    assert violations("two-step, 18 to 30 percent, 3 - 1") == []


def test_a_phrase_broken_across_lines_still_counts() -> None:
    assert violations("we will dive\n  into the numbers") == [(1, "dive into")]
    assert violations("Here’s the thing") == [(1, "here's the thing")]


def test_whole_words_only() -> None:
    """The list is written as whole words, so a longer word is not a hit."""
    assert violations("unlocked, robustness, elevated, showcased") == []
    assert violations("the cutting-edge model") == [(1, "cutting-edge")]


def test_the_mock_model_file_uses_no_word_a_card_back_may_not() -> None:
    """Its word lists end up on card backs, so the file answers to the card-back
    list in text.py as well. text.py itself holds that list, and the prompt names
    it, so text.py is read for the page list only, above."""
    assert phrases_in(MOCK_LLM.read_text(encoding="utf-8"), BANNED + PRAISE) == []
    assert TEXT_PY in FILES
    assert MOCK_LLM in FILES
