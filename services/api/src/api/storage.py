"""Where a card's files live on the data volume.

    /data/cards.db                      the job database
    /data/uploads/<card_id>/photo.jpg   the photo, EXIF stripped, downscaled
                            face.json   the face box the browser sent, or null
    /data/cards/<card_id>/front.png     the finished card
                          back.png
                          preview.jpg
    /data/work/<card_id>/cutout.png     intermediates, kept for debugging until
                         art.png        the sweeper or DELETE removes them
                         text.json

One card id, three directories, one function that deletes all of them. Card ids
are 128-bit tokens from secrets.token_urlsafe, so a caller cannot guess another
card's id, but `is_card_id` still refuses anything with a slash or a dot in it
before it is ever joined to a path.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

# secrets.token_urlsafe(16) gives 22 characters from this alphabet.
CARD_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

ASSETS = {"front.png": "image/png", "back.png": "image/png", "preview.jpg": "image/jpeg"}

# face.json holds these four keys, in the stored photo's pixels, or the JSON null.
FACE_KEYS = ("x", "y", "width", "height")

Box = tuple[int, int, int, int]
"""A face box as the pipeline uses it: x, y, width, height."""


def is_card_id(value: str) -> bool:
    """True for something that could be a card id. Path separators are not."""
    return bool(CARD_ID.match(value))


class Storage:
    """The data volume, addressed by card id."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def db_path(self) -> Path:
        return self.root / "cards.db"

    def upload_dir(self, card_id: str) -> Path:
        return self.root / "uploads" / card_id

    def photo_path(self, card_id: str) -> Path:
        return self.upload_dir(card_id) / "photo.jpg"

    def face_path(self, card_id: str) -> Path:
        return self.upload_dir(card_id) / "face.json"

    def card_dir(self, card_id: str) -> Path:
        return self.root / "cards" / card_id

    def work_dir(self, card_id: str) -> Path:
        return self.root / "work" / card_id

    def asset_path(self, card_id: str, name: str) -> Path:
        return self.card_dir(card_id) / name

    def ready(self, card_id: str) -> list[str]:
        """The card's files that are on disk now, in the order ASSETS names them.

        The render writes each one whole under its name, so a file that is
        here can be served.
        """
        return [name for name in ASSETS if self.asset_path(card_id, name).is_file()]

    def drop_assets(self, card_id: str) -> None:
        """Remove the front, the back and the preview. The rest of the card stays."""
        for name in ASSETS:
            self.asset_path(card_id, name).unlink(missing_ok=True)

    def prepare(self) -> None:
        for name in ("uploads", "cards", "work"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def write_photo(self, card_id: str, data: bytes) -> Path:
        path = self.photo_path(card_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_face(self, card_id: str, face: Box | None) -> Path:
        """The face box beside the photo, in its pixels. None is the browser's null."""
        path = self.face_path(card_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dict(zip(FACE_KEYS, face, strict=True)) if face is not None else None)
        )
        return path

    def read_face(self, card_id: str) -> tuple[bool, Box | None]:
        """(sent, box). `sent` is False when the upload came with no face field.

        The browser always sends one, a box or null. cardctl never does.
        """
        path = self.face_path(card_id)
        if not path.is_file():
            return False, None
        payload = json.loads(path.read_text())
        if payload is None:
            return True, None
        return True, tuple(int(payload[key]) for key in FACE_KEYS)

    def purge(self, card_id: str) -> None:
        """Remove the upload, the intermediates, and the finished card."""
        for directory in (
            self.upload_dir(card_id),
            self.card_dir(card_id),
            self.work_dir(card_id),
        ):
            shutil.rmtree(directory, ignore_errors=True)


__all__ = ["ASSETS", "FACE_KEYS", "Box", "Storage", "is_card_id"]
