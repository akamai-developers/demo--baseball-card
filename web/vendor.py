"""Download the face detector's files, and check them against web/vendor.lock.

    make web-vendor                         download what is missing or wrong
    uv run python web/vendor.py --check     verify, download nothing

The api Dockerfile runs this too, so the image carries the files and the page
never asks a CDN for anything at run time. web/vendor/ is gitignored: someone
else's build does not belong in this repo, and the lock file is what makes the
download the same every time.

Standard library only. It runs in the api image before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
LOCK = WEB_DIR / "vendor.lock"
VENDOR = WEB_DIR / "vendor"

TIMEOUT_S = 120
CHUNK = 1 << 20


def entries(lock: Path = LOCK) -> list[tuple[str, str, str]]:
    """(path, hex digest, url) per line. Blank lines and # comments are skipped.

    The digest is written sha256:<hex>, which says what it is to a reader and
    to tests/security/test_repo_secrets.py, which would take 64 bare hex
    characters for a key.
    """
    rows = []
    for number, line in enumerate(lock.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError(
                f"{lock}:{number}: expected 'path sha256 url', got {len(fields)} fields"
            )
        path, digest, url = fields
        if ".." in path or path.startswith("/"):
            raise ValueError(f"{lock}:{number}: {path} is not a path under web/vendor")
        if not digest.startswith("sha256:"):
            raise ValueError(f"{lock}:{number}: write the digest as sha256:<hex>")
        rows.append((path, digest.removeprefix("sha256:"), url))
    return rows


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, target: Path, expected: str) -> None:
    """Download, hash, then move into place. A wrong hash leaves nothing behind."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "demo--baseball-card/vendor"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response, partial.open("wb") as out:
        while chunk := response.read(CHUNK):
            digest.update(chunk)
            out.write(chunk)
    got = digest.hexdigest()
    if got != expected:
        partial.unlink(missing_ok=True)
        raise ValueError(f"{url}\n  expected sha256 {expected}\n  got      sha256 {got}")
    partial.replace(target)


def run(check_only: bool = False, vendor: Path = VENDOR, lock: Path = LOCK) -> int:
    ok = True
    for path, expected, url in entries(lock):
        target = vendor / path
        if target.is_file() and sha256(target) == expected:
            print(f"ok       {path}")
            continue
        if check_only:
            print(f"MISSING  {path}", file=sys.stderr)
            ok = False
            continue
        print(f"fetch    {path}")
        try:
            fetch(url, target, expected)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            print(f"failed   {path}: {exc}", file=sys.stderr)
            ok = False
    if not ok and check_only:
        print("\nweb/vendor is incomplete. run: make web-vendor", file=sys.stderr)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch web/vendor from web/vendor.lock.")
    parser.add_argument("--check", action="store_true", help="verify only, download nothing")
    args = parser.parse_args(argv)
    return run(check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
