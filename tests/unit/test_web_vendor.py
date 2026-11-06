"""web/vendor.lock and the fetcher that reads it.

The point of the lock file is that the face detector, which this repo did not
write, arrives the same way every time and is checked before it is served.
These tests cover the lock file's shape, the hash check, and the fact that the
api image builds itself a copy rather than trusting one from the build context.
No test here touches the network: the fetcher is pointed at file:// URLs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

WEB = REPO_ROOT / "web"
LOCK = WEB / "vendor.lock"

SHA256 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def vendor():
    """web/vendor.py, loaded by path. It is a script, not a package."""
    spec = importlib.util.spec_from_file_location("web_vendor", WEB / "vendor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the lock file -----------------------------------------------------------


def test_the_lock_file_parses(vendor) -> None:
    rows = vendor.entries(LOCK)
    assert rows, "web/vendor.lock lists nothing"
    for path, digest, url in rows:
        assert SHA256.match(digest), f"{path} has no sha256"
        assert url.startswith("https://"), f"{path} is fetched over {url.split(':')[0]}"


def test_it_pins_the_detector_and_its_model_and_nothing_else(vendor) -> None:
    """The bundle, the wasm loader, the wasm and BlazeFace: what detector.js loads."""
    paths = [path for path, _, _ in vendor.entries(LOCK)]
    assert paths == [
        "tasks-vision/vision_bundle.mjs",
        "tasks-vision/wasm/vision_wasm_internal.js",
        "tasks-vision/wasm/vision_wasm_internal.wasm",
        "models/blaze_face_short_range.tflite",
    ]
    detector = (WEB / "app" / "detector.js").read_text()
    for wanted in (
        '"/vendor/tasks-vision/vision_bundle.mjs"',
        '"/vendor/tasks-vision/wasm"',
        '"/vendor/models/blaze_face_short_range.tflite"',
    ):
        assert wanted in detector, wanted


def test_every_url_carries_a_version(vendor) -> None:
    """An unpinned URL would let the same lock file mean two different builds."""
    for path, _, url in vendor.entries(LOCK):
        assert re.search(r"@\d+\.\d+\.\d+|/\d+/", url), f"{path} is not pinned to a version"


def test_a_path_cannot_escape_the_vendor_directory(vendor, tmp_path) -> None:
    lock = tmp_path / "bad.lock"
    lock.write_text(f"../../etc/passwd  {'a' * 64}  https://example.com/x\n")
    with pytest.raises(ValueError, match="not a path under web/vendor"):
        vendor.entries(lock)


def test_a_digest_must_say_it_is_a_sha256(vendor, tmp_path) -> None:
    lock = tmp_path / "bad.lock"
    lock.write_text(f"sub/thing.js  {'a' * 64}  https://example.com/x\n")
    with pytest.raises(ValueError, match="sha256:<hex>"):
        vendor.entries(lock)


def test_a_malformed_line_is_refused(vendor, tmp_path) -> None:
    lock = tmp_path / "bad.lock"
    lock.write_text("only-two-fields  https://example.com/x\n")
    with pytest.raises(ValueError, match="expected"):
        vendor.entries(lock)


# -- the fetcher -------------------------------------------------------------


def write_lock(tmp_path: Path, source: Path, digest: str) -> Path:
    lock = tmp_path / "vendor.lock"
    lock.write_text(f"sub/thing.js  sha256:{digest}  {source.as_uri()}\n")
    return lock


def source_file(tmp_path: Path, body: bytes = b"console.log('hi')\n") -> tuple[Path, str]:
    source = tmp_path / "thing.js"
    source.write_bytes(body)
    return source, hashlib.sha256(body).hexdigest()


def test_it_downloads_and_verifies(vendor, tmp_path) -> None:
    source, digest = source_file(tmp_path)
    vendor_dir = tmp_path / "vendor"
    assert vendor.run(vendor=vendor_dir, lock=write_lock(tmp_path, source, digest)) == 0
    assert (vendor_dir / "sub" / "thing.js").read_bytes() == source.read_bytes()


def test_a_wrong_hash_leaves_nothing_behind(vendor, tmp_path) -> None:
    source, _ = source_file(tmp_path, b"not what the lock file says\n")
    vendor_dir = tmp_path / "vendor"
    assert vendor.run(vendor=vendor_dir, lock=write_lock(tmp_path, source, "b" * 64)) == 1
    assert not (vendor_dir / "sub" / "thing.js").exists()
    assert not (vendor_dir / "sub" / "thing.js.part").exists()


def test_check_downloads_nothing(vendor, tmp_path) -> None:
    source, digest = source_file(tmp_path)
    vendor_dir = tmp_path / "vendor"
    lock = write_lock(tmp_path, source, digest)
    assert vendor.run(check_only=True, vendor=vendor_dir, lock=lock) == 1
    assert not vendor_dir.exists()


def test_a_file_that_is_already_right_is_not_fetched_again(vendor, tmp_path) -> None:
    source, digest = source_file(tmp_path)
    vendor_dir = tmp_path / "vendor"
    lock = write_lock(tmp_path, source, digest)
    assert vendor.run(vendor=vendor_dir, lock=lock) == 0
    source.unlink()  # a second download would fail now
    assert vendor.run(vendor=vendor_dir, lock=lock) == 0


def test_a_file_that_has_changed_underneath_is_replaced(vendor, tmp_path) -> None:
    source, digest = source_file(tmp_path)
    vendor_dir = tmp_path / "vendor"
    target = vendor_dir / "sub" / "thing.js"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"something else")
    assert vendor.run(vendor=vendor_dir, lock=write_lock(tmp_path, source, digest)) == 0
    assert target.read_bytes() == source.read_bytes()


# -- how it is wired ---------------------------------------------------------


def test_the_api_image_fetches_its_own_copy() -> None:
    """Not a copy from the build context: .dockerignore keeps web/vendor out, and
    the image runs the same script against the same lock file."""
    dockerfile = (REPO_ROOT / "services" / "api" / "Dockerfile").read_text()
    assert "COPY web/vendor.lock web/vendor.py /app/web/" in dockerfile
    assert "RUN python /app/web/vendor.py" in dockerfile
    assert "web/vendor/" in (REPO_ROOT / ".dockerignore").read_text().splitlines()


def test_the_vendor_directory_is_not_committed() -> None:
    assert "web/vendor/" in (REPO_ROOT / ".gitignore").read_text().splitlines()


def test_make_has_a_target_for_it() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "web-vendor:" in makefile
    assert "uv run python web/vendor.py" in makefile
