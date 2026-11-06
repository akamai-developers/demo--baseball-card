"""Where a card's files live, and what DELETE has to remove."""

from __future__ import annotations

import json

import pytest

from api.storage import ASSETS, Storage, is_card_id
from common.schemas import new_card_id


@pytest.fixture
def storage(tmp_path) -> Storage:
    store = Storage(tmp_path / "data")
    store.prepare()
    return store


def test_a_generated_card_id_is_accepted() -> None:
    for _ in range(20):
        assert is_card_id(new_card_id())


@pytest.mark.parametrize(
    "value",
    ["", "..", "../../etc/passwd", "abc/def", "a" * 200, "has space", "dot.dot", "semi;colon"],
)
def test_anything_that_could_escape_the_data_directory_is_refused(value: str) -> None:
    assert not is_card_id(value)


def test_every_asset_has_a_content_type() -> None:
    assert set(ASSETS) == {"front.png", "back.png", "preview.jpg"}
    assert all(ASSETS.values())


def test_paths_are_all_under_the_root(storage: Storage) -> None:
    card_id = new_card_id()
    for path in (
        storage.photo_path(card_id),
        storage.face_path(card_id),
        storage.card_dir(card_id),
        storage.work_dir(card_id),
        storage.db_path,
        storage.asset_path(card_id, "front.png"),
    ):
        assert storage.root in path.parents


def test_writing_a_photo_creates_its_directory(storage: Storage) -> None:
    path = storage.write_photo("abc", b"not really a jpeg")
    assert path.read_bytes() == b"not really a jpeg"


# -- the face box -------------------------------------------------------------


def test_a_face_box_is_stored_beside_the_photo(storage: Storage) -> None:
    path = storage.write_face("abc", (458, 75, 123, 123))
    assert path.parent == storage.photo_path("abc").parent
    assert json.loads(path.read_text()) == {"x": 458, "y": 75, "width": 123, "height": 123}
    assert storage.read_face("abc") == (True, (458, 75, 123, 123))


def test_a_null_is_stored_as_null(storage: Storage) -> None:
    """The browser looked and found no face. That is not the same as not looking."""
    path = storage.write_face("abc", None)
    assert path.read_text() == "null"
    assert storage.read_face("abc") == (True, None)


def test_no_face_file_means_no_face_field_came(storage: Storage) -> None:
    storage.write_photo("abc", b"photo")
    assert storage.read_face("abc") == (False, None)


def test_purge_removes_the_upload_the_work_and_the_card(storage: Storage) -> None:
    storage.write_photo("abc", b"photo")
    storage.write_face("abc", (1, 2, 30, 40))
    storage.work_dir("abc").mkdir(parents=True)
    (storage.work_dir("abc") / "cutout.png").write_bytes(b"cutout")
    storage.card_dir("abc").mkdir(parents=True)
    (storage.card_dir("abc") / "front.png").write_bytes(b"front")

    storage.purge("abc")

    assert not storage.face_path("abc").exists()
    assert not storage.upload_dir("abc").exists()
    assert not storage.work_dir("abc").exists()
    assert not storage.card_dir("abc").exists()


def test_purging_a_card_that_was_never_made_is_quiet(storage: Storage) -> None:
    storage.purge("never-existed")
