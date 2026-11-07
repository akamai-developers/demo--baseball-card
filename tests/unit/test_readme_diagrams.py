"""The README's two diagrams are copies of the ones in docs/ARCHITECTURE.md.

The node, as a box with Caddy's routes and the volumes, and one card, as a
flowchart. A change to either file without the other would leave two pictures
of one system, so this holds them equal.
"""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

README = REPO_ROOT / "README.md"
ARCHITECTURE = REPO_ROOT / "docs" / "ARCHITECTURE.md"


def block(text: str, heading: str, opening: str) -> str:
    """The first fenced block that opens with `opening` after `heading`."""
    found = re.search(
        r"(?ms)^" + re.escape(heading) + r"\n.*?^(" + re.escape(opening) + r"\n.*?^```\n)", text
    )
    assert found, f"no {opening!r} block under {heading!r}"
    return found.group(1)


def test_the_node_diagram_is_the_same_in_both() -> None:
    assert block(README.read_text(), "## how it works", "```\nlaptop") == block(
        ARCHITECTURE.read_text(), "## the node", "```\nlaptop"
    )


def test_the_card_flow_is_the_same_in_both() -> None:
    assert block(README.read_text(), "## how it works", "```mermaid") == block(
        ARCHITECTURE.read_text(), "## one card", "```mermaid"
    )


def test_the_box_is_one_width() -> None:
    """A line that is a character long or short shows as a broken edge."""
    lines = block(ARCHITECTURE.read_text(), "## the node", "```\nlaptop").splitlines()
    edges = {len(line) for line in lines if line[:1] in "|+"}
    assert len(edges) == 1, edges
