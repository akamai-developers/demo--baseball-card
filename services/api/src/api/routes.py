"""The card routes.

    POST   /cards                    multipart: photo file, stats JSON, style,
                                     and an optional face box. 202
    GET    /cards/{id}               status, steps, text, comps, warnings, and
                                     which files are ready
    GET    /cards/{id}/front.png     also back.png and preview.jpg, each as
                                     soon as it is written
    DELETE /cards/{id}               remove the card, its upload and its rows

Every one of them needs the bearer key. The middleware in common.auth checks it
before routing, and each route asks again, so a route added later cannot be
served to a stranger by accident.

POST /cards does the decoding and the downscale, writes the file, and returns.
The pipeline runs as a background task, which is why the answer is 202 and a
card id rather than a card.

The face box is where the browser found the face in the photo it sent. It is
scaled by the same factor as the photo and stored beside it as face.json.
Compose places the person by it, and the photo QA reads it. A card without one
is placed by the top of the silhouette.

A delete of a card still being made removes its rows first, so nothing its
pipeline writes after that lands, then stops its task and waits for it, so the
files Chromium was writing go with the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api import photo as photo_module
from api.db import Database
from api.pipeline import STEPS, Jobs, Pipeline
from api.storage import ASSETS, Box, Storage, is_card_id
from common.auth import require_api_key
from common.schemas import STYLES, CardJob, Stats, new_card_id
from common.settings import Settings, get_settings

log = logging.getLogger("api")

router = APIRouter(dependencies=[Depends(require_api_key)])

NO_STORE = {"Cache-Control": "no-store"}


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_jobs(request: Request) -> Jobs:
    return request.app.state.jobs


def _json_field(name: str, raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"{name} is not valid JSON: {exc.msg} at char {exc.pos}") from exc


def _problems(name: str, exc: ValidationError) -> str:
    """The first five things pydantic found, each with the field it found it in."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or name}: {error['msg']}"
        for error in exc.errors()[:5]
    )


def parse_stats(raw: str) -> Stats:
    """The stats form field. A bad one is the caller's problem, so say what is wrong."""
    payload = _json_field("stats", raw)
    try:
        return Stats.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(400, f"stats is not usable: {_problems('stats', exc)}") from exc


def parse_style(raw: str) -> str:
    """The style form field: one of the styles in packages/card/src/card/styles.json."""
    value = raw.strip()
    if value not in STYLES:
        raise HTTPException(400, f"style is not usable: use one of {', '.join(STYLES)}")
    return value


# A face under this on the stored photo is too small to place anyone by.
MIN_FACE_PX = 16


class FaceBox(BaseModel):
    """Where the browser found the face, in the pixels of the photo it sent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    # Whole pixels, never negative. No photo is a million pixels on a side, and
    # the cap makes a thousand-digit number a 400 instead of a float overflow.
    x: int = Field(ge=0, le=1_000_000)
    y: int = Field(ge=0, le=1_000_000)
    width: int = Field(ge=0, le=1_000_000)
    height: int = Field(ge=0, le=1_000_000)


def parse_face(raw: str, photo: photo_module.Prepared) -> Box | None:
    """The face form field, in the stored photo's pixels. None is the browser's null.

    The box is scaled by the factor the photo was scaled by. Then it has to lie
    inside the photo and be at least MIN_FACE_PX on each side.
    """
    payload = _json_field("face", raw)
    if payload is None:
        return None
    try:
        box = FaceBox.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(400, f"face is not usable: {_problems('face', exc)}") from exc

    scale = photo.scale
    left, top = round(box.x * scale), round(box.y * scale)
    right, bottom = round((box.x + box.width) * scale), round((box.y + box.height) * scale)
    width, height = photo.size
    if right > width or bottom > height:
        raise HTTPException(400, "face is not usable: the box runs past the edge of the photo")
    if right - left < MIN_FACE_PX or bottom - top < MIN_FACE_PX:
        scaled = f" once the photo is scaled to {width} x {height}" if scale < 1 else ""
        raise HTTPException(400, f"face is not usable: a side is under {MIN_FACE_PX} px{scaled}")
    return left, top, right - left, bottom - top


# How long a delete waits for a card it stopped to end. A render is a few
# seconds, and nothing else a card does holds on after a cancel.
STOP_WAIT_S = 15.0


async def _known_card(db: Database, card_id: str) -> CardJob:
    """The job, or a 404. An id that could not exist never reaches the database."""
    if not is_card_id(card_id):
        raise HTTPException(404, "no such card")
    job = await asyncio.to_thread(db.get_job, card_id)
    if job is None:
        raise HTTPException(404, "no such card")
    return job


@router.post("/cards", status_code=status.HTTP_202_ACCEPTED)
async def create_card(
    photo: UploadFile = File(..., description="a headshot against a plain wall"),
    stats: str = Form(..., description="the contents of stats.json"),
    style: str = Form(STYLES[0], description=f"the card's design: {', '.join(STYLES)}"),
    face: str | None = Form(
        None,
        description='where the face is, {"x", "y", "width", "height"} in the pixels of '
        "the photo, or null when no face was found. The card places the person by it",
    ),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    jobs: Jobs = Depends(get_jobs),
) -> JSONResponse:
    """Take a photo and a stats file. Start a job. Answer with its id."""
    parsed = parse_stats(stats)
    chosen = parse_style(style)

    max_bytes = settings.max_upload_mb * 1_000_000
    raw = await photo.read(max_bytes + 1)
    try:
        prepared = await asyncio.to_thread(
            photo_module.prepare, raw, max_bytes=max_bytes, max_px=settings.max_photo_px
        )
    except photo_module.PhotoError as exc:
        raise HTTPException(400, str(exc)) from exc
    box = parse_face(face, prepared) if face is not None else None

    card_id = new_card_id()
    await asyncio.to_thread(storage.write_photo, card_id, prepared.data)
    if face is not None:
        # A box or a null. With no file the pipeline knows no face field came, and
        # it says nothing about a face.
        await asyncio.to_thread(storage.write_face, card_id, box)
    await asyncio.to_thread(db.create_card, card_id, parsed, chosen, STEPS)
    jobs.start(Pipeline(card_id, parsed, chosen, settings, db, storage, slots=jobs.slots))

    return JSONResponse(
        {"card_id": card_id, "status": "queued"},
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Location": f"/cards/{card_id}", **NO_STORE},
    )


@router.get("/cards/{card_id}", response_model=CardJob)
async def read_card(
    card_id: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> JSONResponse:
    """Where the job got to, every step, the text and comps so far, and which
    files can be fetched. The front comes a little before the back."""
    job = await _known_card(db, card_id)
    job.assets = await asyncio.to_thread(storage.ready, card_id)
    return JSONResponse(job.model_dump(), headers=NO_STORE)


@router.get("/cards/{card_id}/{name}")
async def read_asset(
    card_id: str,
    name: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    """front.png, back.png, or preview.jpg, once the render has written it.

    The front and the preview are there a little before the back, while the
    card is still running. The job's `assets` says which are there.
    """
    if name not in ASSETS:
        raise HTTPException(404, f"no such file. try one of: {', '.join(ASSETS)}")
    job = await _known_card(db, card_id)
    path = storage.asset_path(card_id, name)
    if not path.is_file():
        raise HTTPException(404, f"{name} is not ready. the card is {job.status}")
    return FileResponse(path, media_type=ASSETS[name], headers=NO_STORE)


@router.delete("/cards/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_card(
    card_id: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    jobs: Jobs = Depends(get_jobs),
) -> None:
    """Remove the card, the intermediates, and the photo that made it.

    A card still being made is stopped first, and its task given time to end,
    so nothing it was writing lands after its files are gone.
    """
    await _known_card(db, card_id)
    await asyncio.to_thread(db.delete_card, card_id)
    task = jobs.cancel(card_id)
    if task is not None:
        await asyncio.wait([task], timeout=STOP_WAIT_S)
    await asyncio.to_thread(storage.purge, card_id)
    log.info("card %s purged", card_id)


__all__ = [
    "MIN_FACE_PX",
    "STOP_WAIT_S",
    "FaceBox",
    "parse_face",
    "parse_stats",
    "parse_style",
    "router",
]
