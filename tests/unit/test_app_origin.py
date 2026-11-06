"""docs/SECURITY.md: the page talks to its own origin and nowhere else.

The claim's own check is a grep over web/app for an absolute URL, which prints
nothing. How it works links to each model's repo, so those addresses come from
GET /models and are not written in web/app. This runs the same grep, so a link
typed into the page fails here before it reaches the doc.
"""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

WEB_APP = REPO_ROOT / "web" / "app"

# The pattern in docs/SECURITY.md: grep -rnoE 'https?://[a-zA-Z0-9.-]+'
ABSOLUTE = re.compile(r"https?://[a-zA-Z0-9.-]+")


def test_the_page_names_no_other_origin() -> None:
    files = [
        path for suffix in ("*.js", "*.css", "*.html") for path in sorted(WEB_APP.rglob(suffix))
    ]
    assert len(files) >= 13
    hits = [
        f"{path.relative_to(WEB_APP)}:{number}: {match.group(0)}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for match in ABSOLUTE.finditer(line)
    ]
    assert hits == []


def test_the_doc_still_says_so() -> None:
    security = (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    assert "grep -rnoE 'https?://[a-zA-Z0-9.-]+' web/app" in security
