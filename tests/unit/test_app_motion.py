"""One motion system, checked by reading the files.

The durations and the two curves are custom properties at the top of
web/app/app.css. Every CSS transition and animation in the app is written on
them, and every animation started from JavaScript goes through web/app/
motion.js, which reads them and asks prefers-reduced-motion first. This holds
the files to that: no duration or curve written down anywhere else, and no
element.animate() outside motion.js.

The browser tests in tests/integration/ show the motion itself. This keeps a
new rule from bringing its own number.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import REPO_ROOT

WEB_APP = REPO_ROOT / "web" / "app"

TABLE = {
    "--ak-dur-fast": "150ms",
    "--ak-dur-base": "200ms",
    "--ak-dur-slow": "350ms",
    "--ak-dur-flip": "500ms",
    "--ak-dur-pulse": "1200ms",
    "--ak-ease-out": "cubic-bezier(0.22, 1, 0.36, 1)",
    "--ak-ease-in-out": "cubic-bezier(0.65, 0, 0.35, 1)",
}

CURVES = r"\b(?:ease|ease-in|ease-out|ease-in-out|cubic-bezier|steps|step-start|step-end)\b"


def css() -> str:
    """app.css without its comments."""
    return re.sub(r"/\*.*?\*/", "", (WEB_APP / "app.css").read_text(), flags=re.S)


def block(text: str, opener: str) -> str:
    """The body of the first `opener { ... }`, braces balanced."""
    start = text.index(opener)
    at = text.index("{", start) + 1
    depth = 1
    for index in range(at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[at:index]
    raise AssertionError(f"{opener} is not closed")


ROOT = block(css(), ":root")
REDUCED = block(css(), "@media (prefers-reduced-motion: reduce)")


def items(value: str) -> list[str]:
    """A shorthand's comma-separated items. A comma inside var() is not one."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(value):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    return [*parts, value[start:]]


def motion_rules() -> list[tuple[str, str]]:
    """Every transition and animation declaration outside the tokens and the
    reduced-motion rule, as (property, value)."""
    rest = css().replace(ROOT, "").replace(REDUCED, "")
    return re.findall(r"(?<![\w-])((?:transition|animation)(?:-[a-z-]+)?)\s*:\s*([^;{}]+);", rest)


def code(path: Path) -> str:
    """A module without its comments. Comments may name the API; code may not call it."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return re.sub(r"(?<![:\"'])//[^\n]*", "", text)


def test_the_table_is_the_tokens() -> None:
    for name, value in TABLE.items():
        assert re.search(rf"{name}:\s*{re.escape(value)};", ROOT), name


def test_there_are_rules_to_check() -> None:
    """The pattern found the transitions, so the tests below are not vacuous."""
    names = {name for name, _ in motion_rules()}
    assert {"transition", "animation"} <= names
    assert len(motion_rules()) >= 10


def test_every_duration_is_a_token() -> None:
    for name, value in motion_rules():
        bare = re.sub(r"var\([^)]*\)", "", value)
        assert not re.search(r"(?<![\w.-])\d*\.?\d+m?s\b", bare), f"{name}: {value}"
        if name in ("transition", "animation"):
            for part in items(value):
                assert "var(--ak-dur-" in part, f"{name}: {part.strip()} has no duration token"


def test_every_curve_is_a_token() -> None:
    for name, value in motion_rules():
        bare = re.sub(r"var\([^)]*\)", "", value)
        assert not re.search(CURVES, bare), f"{name}: {value}"


def test_reduced_motion_stops_everything() -> None:
    for line in (
        "transition-property: none !important",
        "transition-duration: 1ms !important",
        "animation-name: none !important",
        "animation-duration: 1ms !important",
    ):
        assert line in REDUCED, line


def test_javascript_animates_only_through_motion_js() -> None:
    for path in sorted(WEB_APP.glob("*.js")):
        if path.name == "motion.js":
            continue
        text = code(path)
        assert not re.search(r"(?<!motion)\.animate\(", text), path.name
        assert "prefers-reduced-motion" not in text, path.name
        assert not re.search(r"\bduration\s*:\s*\d", text), path.name


def test_motion_js_asks_before_it_animates() -> None:
    text = (WEB_APP / "motion.js").read_text()
    body = text[text.index("export function animate(") :]
    body = body[: body.index("\n}\n")]
    assert body.index("reduced()") < body.index("element.animate(")
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in text
