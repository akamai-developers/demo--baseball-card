"""The backgrounds the art service keeps ready, so a card does not wait for FLUX.

    <DATA_DIR>/pool/<style>-<recipe>/<painted_ns>-<seed>-<ms>.png

ART_POOL_SIZE finished backgrounds per style, 3 by default. A card that asks
for its style's recipe takes the newest one at once, and the painter paints a
replacement when nothing else wants the model. A request that is not a style's
recipe, or says `"fresh": true`, is painted on the spot.

Three parts:

  Pool     the files, and an index of them in memory. Only the event loop
           touches the index, and never across an await, so two cards cannot
           take the same file.
  Turns    who is on the model. A request goes before the painter, and a
           painting in progress gives way at the end of its current step.
  Keeper   the painter. It waits until the model has been free for QUIET_S,
           paints the style with the fewest ready, and starts again.

`<recipe>` is a hash of what decides the picture apart from the seed: the
prompt, the size, the steps, the model, its revision and the card it runs on.
Change any of them and the old files are no longer served. The next start
deletes them.

The pictures are stadiums. They hold nothing about anyone, and they are not
swept with the cards: they wait for the next card, however long that is.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

log = logging.getLogger("art.pool")

# How long the model must have been free before the painter starts. A card's
# own painting often follows another request within a second, and it should
# not find the painter in its way.
QUIET_S = 1.0
# How often the painter looks again while the model is busy.
POLL_S = 0.25
# After a painting fails, wait this long, doubling to BACKOFF_MAX_S.
BACKOFF_S = 5.0
BACKOFF_MAX_S = 300.0

SUFFIX = ".png"
PART = ".part"


@dataclass(frozen=True)
class Entry:
    """One finished background on disk."""

    path: Path
    style: str
    seed: int
    painted_ns: int
    # How long the painting took, for X-Infer-Ms.
    ms: int

    @property
    def name(self) -> str:
        return self.path.name


def entry_name(painted_ns: int, seed: int, ms: int) -> str:
    """Zero-padded time first, so names sort in the order they were painted."""
    return f"{painted_ns:020d}-{seed}-{ms}{SUFFIX}"


def parse_entry(path: Path, style: str) -> Entry | None:
    """An Entry from a file name, or None for a file this code did not write."""
    if path.suffix != SUFFIX:
        return None
    parts = path.stem.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    painted_ns, seed, ms = (int(part) for part in parts)
    return Entry(path=path, style=style, seed=seed, painted_ns=painted_ns, ms=ms)


def recipe_key(recipe: dict, model: str, revision: str, ran_on: str) -> str:
    """16 hex characters over everything that decides the picture but the seed."""
    material = json.dumps(
        {
            "prompt": recipe["prompt"],
            "width": recipe["width"],
            "height": recipe["height"],
            "steps": recipe["steps"],
            "model": model,
            "revision": revision,
            "ran_on": ran_on,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class Pool:
    """The files under `root`, one directory per style, and an index of them.

    `size` per style. The index is a list per style in the order the files were
    painted, so the newest is the last.
    """

    def __init__(self, root: Path, size: int, recipes: dict[str, dict], keys: dict[str, str]):
        self.root = root
        self.size = size
        self.recipes = recipes
        self.dirs = {style: root / f"{style}-{keys[style]}" for style in recipes}
        self.index: dict[str, list[Entry]] = {style: [] for style in recipes}

    # -- disk, called in a thread --------------------------------------------

    def load(self) -> None:
        """Make the directories, read what is there, and delete what is stale.

        Stale is a directory for a recipe this service no longer paints, a
        half-written file, and anything past `size` in a style, oldest first.
        Only files this code writes are deleted. Raises OSError when the root
        cannot be made, and the service then runs without a pool.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        wanted = {directory.name for directory in self.dirs.values()}
        for child in self.root.iterdir():
            if child.is_dir() and child.name not in wanted:
                _clear(child)
                log.info("pool: dropped %s, a recipe this service no longer paints", child.name)

        for style, directory in self.dirs.items():
            directory.mkdir(exist_ok=True)
            for leftover in directory.glob(f"*{PART}"):
                leftover.unlink(missing_ok=True)
            found = sorted(
                (entry for path in directory.iterdir() if (entry := parse_entry(path, style))),
                key=lambda entry: entry.name,
            )
            extra = max(0, len(found) - self.size)
            for entry in found[:extra]:
                entry.path.unlink(missing_ok=True)
            self.index[style] = found[extra:]

    def write(self, style: str, png: bytes, seed: int, ms: int) -> Entry:
        """Put one finished background on disk. Written aside, then renamed."""
        painted_ns = time.time_ns()
        path = self.dirs[style] / entry_name(painted_ns, seed, ms)
        part = path.with_suffix(PART)
        part.write_bytes(png)
        os.replace(part, path)
        return Entry(path=path, style=style, seed=seed, painted_ns=painted_ns, ms=ms)

    @staticmethod
    def read(entry: Entry) -> bytes | None:
        """The PNG, and the file gone. None when it was already gone."""
        try:
            data = entry.path.read_bytes()
        except OSError as exc:
            log.warning("pool: could not read %s. %s", entry.path, exc)
            return None
        try:
            entry.path.unlink()
        except OSError as exc:
            # Served once already. Leaving it on disk would serve it again after
            # a restart, which is a repeat, not a failure.
            log.warning("pool: could not delete %s. %s", entry.path, exc)
        return data

    # -- the index, on the event loop only -----------------------------------

    def ready(self, style: str) -> int:
        return len(self.index.get(style, ()))

    def counts(self) -> dict[str, int]:
        return {style: len(entries) for style, entries in self.index.items()}

    def total(self) -> int:
        return sum(len(entries) for entries in self.index.values())

    def target(self) -> int:
        return self.size * len(self.recipes)

    def wanted(self) -> str | None:
        """The style with the fewest ready, below `size`. Ties go in styles.json order."""
        short = [style for style in self.recipes if self.ready(style) < self.size]
        if not short:
            return None
        return min(short, key=lambda style: self.ready(style))

    def add(self, entry: Entry) -> None:
        self.index[entry.style].append(entry)

    def take(self, style: str) -> Entry | None:
        """The newest background for `style`, out of the index. None when there is none."""
        entries = self.index.get(style)
        if not entries:
            return None
        return entries.pop()

    def matches(self, request) -> bool:
        """Whether a request asks for exactly a style's recipe. Only those are pooled."""
        recipe = self.recipes.get(request.style)
        if recipe is None:
            return False
        return (request.prompt, request.width, request.height, request.steps) == (
            recipe["prompt"],
            recipe["width"],
            recipe["height"],
            recipe["steps"],
        )


def _clear(directory: Path) -> None:
    """Delete the files this code writes in `directory`, then the directory if empty."""
    for path in directory.iterdir():
        if path.is_file() and path.suffix in (SUFFIX, PART):
            path.unlink(missing_ok=True)
    # Something else is in there when this fails. Not ours to delete.
    with contextlib.suppress(OSError):
        directory.rmdir()


class Turns:
    """Who is on the model. Requests first, and the painter when nobody asks.

    `lock` is the service's own lock, held across every inference. A request
    that arrives while the painter holds it sets `give_way`, and the painting
    stops at the end of its current denoising step. So a request waits for at
    most one step and the VAE, never a whole painting.
    """

    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock = lock
        # Read by the painting thread, between steps.
        self.give_way = threading.Event()
        self._requests = 0
        self._painting = False
        self._free_since = time.monotonic()

    @asynccontextmanager
    async def request(self) -> AsyncIterator[None]:
        """Hold the model for one request. Counted from the moment it starts waiting."""
        self._requests += 1
        if self._painting:
            self.give_way.set()
        try:
            async with self.lock:
                yield
        finally:
            self._requests -= 1
            self._free_since = time.monotonic()

    def idle_for(self) -> float | None:
        """Seconds since the last request let go. None while one holds or waits."""
        if self._requests or self.lock.locked():
            return None
        return time.monotonic() - self._free_since

    @asynccontextmanager
    async def painting(self) -> AsyncIterator[threading.Event]:
        """Hold the model for one painting. Call it only when idle_for() says so:
        acquiring a free lock with nobody waiting does not yield, so nothing can
        get in between the check and this."""
        async with self.lock:
            self.give_way.clear()
            self._painting = True
            try:
                yield self.give_way
            finally:
                self._painting = False


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class Keeper:
    """Keeps the pool full. One per art service.

    `paint(request, give_way)` is the service's own painter. It returns None
    when it gave way before the last step, and that picture is thrown away.
    `make_request(style, seed)` builds the request for a style's recipe.
    """

    def __init__(
        self,
        pool: Pool,
        turns: Turns,
        paint: Callable,
        make_request: Callable[[str, int], object],
    ) -> None:
        self.pool = pool
        self.turns = turns
        self.paint = paint
        self.make_request = make_request
        self.poked = asyncio.Event()
        self.task: asyncio.Task | None = None
        # For the tests: what the painter last did.
        self.painted = 0
        self.gave_way = 0
        self.last_error: str | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="art-pool")

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    def poke(self) -> None:
        """A background left the pool. Look again."""
        self.poked.set()

    async def run(self) -> None:
        backoff = BACKOFF_S
        while True:
            style = self.pool.wanted()
            if style is None:
                self.poked.clear()
                await self.poked.wait()
                continue

            idle = self.turns.idle_for()
            if idle is None:
                await asyncio.sleep(POLL_S)
                continue
            if idle < QUIET_S:
                await asyncio.sleep(QUIET_S - idle)
                continue

            seed = secrets.randbelow(2**31)
            request = self.make_request(style, seed)
            started = time.perf_counter()
            try:
                async with self.turns.painting() as give_way:
                    image = await asyncio.to_thread(self.paint, request, give_way)
                if image is None:
                    # A request came in. Its turn now, and this style is still
                    # the one to paint when the model is free again.
                    self.gave_way += 1
                    continue
                ms = int(round((time.perf_counter() - started) * 1000))
                png = await asyncio.to_thread(png_bytes, image)
                entry = await asyncio.to_thread(self.pool.write, style, png, seed, ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the pool is a nicety. The service stays up
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "pool: painting %s failed, next try in %.0fs. %s",
                    style,
                    backoff,
                    self.last_error,
                )
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
                continue

            self.pool.add(entry)
            self.painted += 1
            self.last_error = None
            backoff = BACKOFF_S


__all__ = [
    "BACKOFF_MAX_S",
    "BACKOFF_S",
    "POLL_S",
    "QUIET_S",
    "Entry",
    "Keeper",
    "Pool",
    "Turns",
    "entry_name",
    "parse_entry",
    "png_bytes",
    "recipe_key",
]
