"""One card, start to finish.

    photo + stats
      -> cutout    BiRefNet, or the plain-wall matte in mock mode
      -> art       FLUX.2 klein, from the art service's pool or painted now,
                   or a drawn stadium when it does not answer
      -> comps     nearest neighbour over the Statcast hitters, numpy, no model
      -> text      Gemma 4 on vLLM, or a template
      -> compose   Pillow and OpenCV put the cutout over the art
      -> render    Chromium lays out every word, the front and the back at once

Cutout, art, comps and text do not depend on each other, so they run at the
same time. The text is written from the stats alone: the model is not told who
the comps are. Each branch has its own ceiling and its own fallback, and none
of them can stop the card: a failed cutout leaves the background, a failed art
step gets a drawn stadium, a failed comps lookup drops the "Swings like"
section, and a failed text step, or text that fails its checks twice, gets the
template. The only step that can fail the whole job is the render itself.

The render writes front.png and preview.jpg the moment Chromium has drawn the
front, a little before back.png. GET /cards/{id} lists the files that are there
in `assets`, so the app shows the front while the card is still running.

Each step's row in the ledger is built from the headers the model service sent,
so the line on the back of the card is what that service said about itself.

The matte the cutout returns is also read for how the photo was framed. Those
warnings go on the job and are shown next to the card in the app. They never
fail a job: a card always comes out.

Compose places the person by their face on every card. When the browser found
the face, its box is in face.json beside the photo, in the photo's pixels,
which the cutout shares. Without one, the top of the silhouette stands in for
it. After compose, the job says where the face landed when it is not where the
rule puts it: placed low or high, face small or large. When the browser sent a
null, or a box that is not on the person, the job says no face was found. A
post with no face field, which is what cardctl sends, never says that.

DELETE /cards/{id} removes the card's rows, then cancels its task. The task
stops at its next await. A thread cannot be stopped, so a thread the job
started finishes first: blocking(). With the rows gone, nothing the job writes
on its way out lands anywhere.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from contextlib import nullcontext

import httpx
from PIL import Image

from api import qa
from api.art import DRAWN, art_request, fallback, seed_for
from api.clients import Reply, ServiceError, post_art, post_cutout
from api.db import Database
from api.storage import Box, Storage
from api.text import generate, template_text
from card.compose import compose_hero
from card.render import render_card
from common.schemas import CardText, Comp, Stats
from common.settings import Settings
from comps.nn import lookup

log = logging.getLogger("pipeline")

# The ledger, in the order it appears on the job.
STEPS = ("cutout", "art", "comps", "text", "compose", "render")

# What the comps step puts in the model column. It is a numpy lookup, not a
# model, and the licence column says so. card/render.py:MODEL_STEPS leaves it
# out of the rule that decides the line about GPUs on the back, because a CPU
# lookup has nothing to say about whether a GPU was involved.
COMPS_MODEL = "nearest neighbour"

# The names the routes serve. render_card writes each file under its name the
# moment it is done, the front and its preview a little before the back, so
# GET /cards/{id} can say the front is there while the card is still running.
ASSET_NAMES = {"front": "front.png", "back": "back.png", "preview": "preview.jpg"}


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


async def blocking(func, /, *args, **kwargs):
    """asyncio.to_thread, except that a cancelled job waits for the thread to end.

    A thread cannot be stopped. Without this, a card deleted in the middle of
    its render would have Chromium write the card's files after the api had
    purged its directory, and nothing would ever sweep them.
    """
    work = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await asyncio.wait([work])
        if not work.cancelled():
            work.exception()  # read, so asyncio does not report it as lost
        raise


class Pipeline:
    """Runs one job. Holds nothing after it returns."""

    def __init__(
        self,
        card_id: str,
        stats: Stats,
        style: str,
        settings: Settings,
        db: Database,
        storage: Storage,
        slots: asyncio.Semaphore | None = None,
    ):
        self.card_id = card_id
        self.stats = stats
        self.style = style
        self.settings = settings
        self.db = db
        self.storage = storage
        # Chromium is a few hundred MB of resident memory per render. Jobs queue
        # here rather than all starting a browser page at once. None means no
        # limit, which is what a single-job test wants.
        self.slots = slots

    # -- ledger --------------------------------------------------------------

    async def _set(self, step: str, **fields: object) -> None:
        await blocking(self.db.update_step, self.card_id, step, **fields)

    async def _failed(self, step: str, started: float, why: object) -> None:
        message = f"{type(why).__name__}: {why}" if isinstance(why, Exception) else str(why)
        log.warning("card %s step %s failed. %s", self.card_id, step, message)
        await self._set(step, status="failed", ms=_elapsed_ms(started), error=message)

    def _from_service(self, meta: dict, started: float) -> dict:
        """Step columns from a service's headers. Our clock covers what it left out."""
        return {
            "model": meta.get("model"),
            "license": meta.get("license"),
            "ran_on": meta.get("ran_on"),
            "ms": meta.get("ms") or _elapsed_ms(started),
        }

    def _keep(self, name: str, data: bytes) -> None:
        """Write an intermediate to work/ so a bad card can be looked at later."""
        work = self.storage.work_dir(self.card_id)
        work.mkdir(parents=True, exist_ok=True)
        (work / name).write_bytes(data)

    async def _warn(self, warnings: list[str]) -> None:
        await blocking(self.db.add_warnings, self.card_id, warnings)

    # -- the four branches ---------------------------------------------------

    async def cutout(self, client: httpx.AsyncClient, photo: bytes) -> Image.Image | None:
        """The person, cut out of the photo. None means the card is art only."""
        started = time.perf_counter()
        url = self.settings.cutout_url
        if not url:
            await self._set("cutout", status="skipped", error="no cutout service configured")
            return None

        await self._set("cutout", status="running")
        try:
            reply = await asyncio.wait_for(
                post_cutout(client, url, self.settings.card_api_key, photo),
                timeout=self.settings.step_timeout_s,
            )
            image = Image.open(io.BytesIO(reply.body)).convert("RGBA")
            image.load()
        except (ServiceError, httpx.HTTPError, TimeoutError, OSError) as exc:
            await self._failed("cutout", started, exc)
            return None

        self._keep("cutout.png", reply.body)
        await self._set("cutout", status="done", **self._from_service(reply.meta, started))
        await self.photo_qa(image)
        return image

    async def photo_qa(self, cutout: Image.Image) -> list[str]:
        """Read the matte and say how the photo was framed. Never fails the card."""
        try:
            warnings = await blocking(qa.check_cutout, cutout)
        except Exception as exc:  # a QA bug must not cost someone their card
            log.warning("card %s photo qa did not run. %s", self.card_id, exc)
            return []
        if warnings:
            await self._warn(warnings)
            log.info("card %s photo qa: %s", self.card_id, ", ".join(warnings))
        return warnings

    async def face_qa(self, cutout: Image.Image | None, face: Box | None, sent: bool) -> list[str]:
        """Say where the face landed, and when no face was found. Never fails the card.

        Added after the photo QA warnings, not over them.
        """
        try:
            warnings = await blocking(qa.check_face, cutout, face, sent)
        except Exception as exc:  # a QA bug must not cost someone their card
            log.warning("card %s face check did not run. %s", self.card_id, exc)
            return []
        if warnings:
            await self._warn(warnings)
            log.info("card %s face check: %s", self.card_id, ", ".join(warnings))
        return warnings

    async def _painted(self, reply: Reply, started: float) -> Image.Image:
        """The art service's background, decoded, kept, and in the ledger.

        `source` is pool or fresh. A pooled one was painted before the card
        asked, so its X-Infer-Ms is how long that painting took, and the card
        did not wait for it. The row gets this card's own wait instead.
        """
        image = Image.open(io.BytesIO(reply.body)).convert("RGB")
        image.load()
        self._keep("art.png", reply.body)
        fields = self._from_service(reply.meta, started)
        source = reply.meta.get("source")
        if source == "pool":
            fields["ms"] = _elapsed_ms(started)
        await self._set("art", status="done", source=source, **fields)
        return image

    async def art(self, client: httpx.AsyncClient) -> Image.Image:
        """The background. This one always returns an image, one way or another."""
        started = time.perf_counter()
        url = self.settings.art_url
        problem = "no art service configured"

        if url:
            await self._set("art", status="running")
            try:
                reply = await asyncio.wait_for(
                    post_art(
                        client,
                        url,
                        self.settings.card_api_key,
                        art_request(self.card_id, self.style),
                    ),
                    timeout=self.settings.art_timeout_s,
                )
                return await self._painted(reply, started)
            except (ServiceError, httpx.HTTPError, TimeoutError, OSError) as exc:
                problem = f"{type(exc).__name__}: {exc}"
                log.warning("card %s art fell back. %s", self.card_id, problem)

        image = await blocking(fallback, self.style, seed_for(self.card_id, self.style))
        # The step is done because the card has a background. `error` says why it
        # is not the one the model would have painted.
        await self._set(
            "art",
            status="done",
            model=DRAWN,
            license="n/a",
            ran_on=self.settings.gpu_label,
            ms=_elapsed_ms(started),
            error=problem,
        )
        return image

    async def comps(self) -> list[Comp]:
        """The hitters whose numbers are closest to these. numpy, no model.

        Returns an empty list for anything that goes wrong, which leaves the
        "Swings like" section off the back of the card.
        """
        started = time.perf_counter()
        if not self.stats.metrics:
            await self._set("comps", status="skipped", error="no metrics were submitted")
            return []
        if self.settings.comps_count == 0:
            await self._set("comps", status="skipped", error="COMPS_COUNT is 0")
            return []

        await self._set("comps", status="running")
        try:
            found = await blocking(lookup, self.stats.metrics, self.settings.comps_count)
        except (OSError, ValueError) as exc:
            await self._failed("comps", started, exc)
            return []

        if not found:
            await self._set(
                "comps",
                status="skipped",
                ms=_elapsed_ms(started),
                error="no submitted metric is in the Statcast table",
            )
            return []

        await self._set(
            "comps",
            status="done",
            model=COMPS_MODEL,
            license="n/a",
            ran_on=self.settings.gpu_label,
            ms=_elapsed_ms(started),
        )
        return found

    async def text(self, client: httpx.AsyncClient) -> CardText:
        """The nickname, the tagline and the blurb, from the stats alone."""
        started = time.perf_counter()

        url = self.settings.llm_url
        if not url:
            await self._set("text", status="skipped", error="no llm service configured")
            return template_text(self.stats)

        await self._set("text", status="running")
        try:
            result = await asyncio.wait_for(
                generate(
                    client,
                    url,
                    self.settings.card_api_key,
                    self.settings.llm_model,
                    self.stats,
                ),
                timeout=self.settings.step_timeout_s,
            )
        except TimeoutError:
            await self._failed("text", started, "the llm did not answer in time")
            return template_text(self.stats)

        # Each answer and what check() found wrong with it, for reading a back
        # that came out as the template.
        if result.meta.get("tries"):
            self._keep("text.json", json.dumps(result.meta["tries"], indent=2).encode())
        if result.from_model:
            # vLLM sends no X- headers. The licence and the GPU label come from
            # settings, and the model id comes from the reply body.
            meta = dict(result.meta)
            meta["license"] = meta.get("license") or self.settings.llm_license or None
            meta["ran_on"] = meta.get("ran_on") or self.settings.llm_ran_on or None
            await self._set("text", status="done", **self._from_service(meta, started))
        else:
            await self._failed("text", started, result.error or "the llm reply was unusable")
        return result.text

    # -- the end -------------------------------------------------------------

    async def finish(
        self,
        art: Image.Image,
        cutout: Image.Image | None,
        text: CardText,
        comps: list[Comp],
    ) -> None:
        """Compose the front picture, then render both sides.

        Where the face landed is read while the card renders: the check only
        adds warnings, and the render does not need them.
        """
        started = time.perf_counter()
        await self._set("compose", status="running")
        sent, face = await blocking(self.storage.read_face, self.card_id)
        hero = await blocking(compose_hero, art, cutout, face=face)
        await self._set(
            "compose", status="done", ms=_elapsed_ms(started), ran_on=self.settings.gpu_label
        )
        checking = asyncio.ensure_future(self.face_qa(cutout, face, sent))
        try:
            await self.render(hero, text, comps)
        finally:
            # However the render ended. The check never raises.
            await asyncio.wait([checking])

    async def render(self, hero: Image.Image, text: CardText, comps: list[Comp]) -> None:
        """Both sides, each file under the name the routes serve as soon as it is done.

        A card that does not finish keeps none of them. A render cut off by a
        delete runs to its end anyway, since a thread cannot be stopped, and one
        that failed may have written the front before the back gave out. Its
        files go once the thread is done with them.
        """
        started = time.perf_counter()
        await self._set("render", status="running")
        job = await blocking(self.db.get_job, self.card_id)
        out_dir = self.storage.card_dir(self.card_id)

        async with self.slots or nullcontext():
            try:
                await blocking(
                    render_card,
                    self.stats,
                    style=self.style,
                    art=hero,
                    cutout=None,  # already composited into hero, above
                    out_dir=out_dir,
                    text=text,
                    comps=comps,
                    steps=job.steps if job else [],
                    names=ASSET_NAMES,
                )
            except BaseException:
                self.storage.drop_assets(self.card_id)
                raise

        await self._set(
            "render", status="done", ms=_elapsed_ms(started), ran_on=self.settings.gpu_label
        )

    async def run(self) -> None:
        """The whole job. Raises nothing the caller has to handle.

        A delete is the one thing that ends it early, as the CancelledError that
        ends its task. DELETE /cards/{id} has removed the card's rows by then.
        """
        photo_path = self.storage.photo_path(self.card_id)
        try:
            photo = await blocking(photo_path.read_bytes)
        except OSError as exc:
            await blocking(self.db.set_status, self.card_id, "failed", f"the upload is gone: {exc}")
            return

        await blocking(self.db.set_status, self.card_id, "running")
        began = time.perf_counter()
        try:
            timeout = httpx.Timeout(self.settings.step_timeout_s, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                cutout, art, comps, text = await asyncio.gather(
                    self.cutout(client, photo), self.art(client), self.comps(), self.text(client)
                )
            await blocking(self.db.set_text, self.card_id, text)
            await blocking(self.db.set_comps, self.card_id, comps)
            await self.finish(art, cutout, text, comps)
        except asyncio.CancelledError:
            log.info("card %s stopped after %.1fs", self.card_id, time.perf_counter() - began)
            raise
        except Exception as exc:
            log.exception("card %s failed", self.card_id)
            await blocking(
                self.db.set_status, self.card_id, "failed", f"{type(exc).__name__}: {exc}"
            )
            return

        await blocking(self.db.set_status, self.card_id, "done")
        log.info("card %s done in %.1fs", self.card_id, time.perf_counter() - began)


class Jobs:
    """Holds a reference to every running job, by card id.

    asyncio only keeps a weak reference to a task, so a job nobody is holding can
    be collected halfway through. This is the strong reference, and the way to a
    card's task when it is deleted.
    """

    def __init__(self, render_slots: int = 2) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        # Built here, inside the running loop, not at import time.
        self.slots = asyncio.Semaphore(render_slots)

    def start(self, pipeline: Pipeline) -> asyncio.Task:
        card_id = pipeline.card_id
        task = asyncio.create_task(pipeline.run(), name=f"card-{card_id}")
        self._tasks[card_id] = task

        def forget(done: asyncio.Task) -> None:
            if self._tasks.get(card_id) is done:
                del self._tasks[card_id]

        task.add_done_callback(forget)
        return task

    def cancel(self, card_id: str) -> asyncio.Task | None:
        """Cancel the card's task, when this process is running one.

        Returns the task, so the caller can wait for it to end. It ends at its
        next await, once any thread it started has finished: blocking().
        """
        task = self._tasks.get(card_id)
        if task is None or task.done():
            return None
        task.cancel()
        return task

    @property
    def running(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout: float = 30.0) -> None:
        """Let jobs in flight finish when the process is shutting down."""
        if self._tasks:
            await asyncio.wait(set(self._tasks.values()), timeout=timeout)


__all__ = ["ASSET_NAMES", "COMPS_MODEL", "STEPS", "Jobs", "Pipeline", "blocking"]
