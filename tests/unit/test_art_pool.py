"""The art service's pool: backgrounds painted ahead of the cards that want them.

Three parts, tested here without a model. The files and their index (Pool), who
is on the model (Turns), and the painter that keeps the pool full (Keeper). Then
the route, in process, with the mock backend and a pool of one per style.

What these guard against: a card waiting for FLUX when a background was ready,
two cards handed the same file, a painter that holds the model while a card
waits for it, a stale picture served after the prompt changed, and a developer
who asked for a lighthouse getting a stadium.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from art import pool as pool_module
from art.pool import (
    Keeper,
    Pool,
    Turns,
    entry_name,
    parse_entry,
    recipe_key,
)
from card.backgrounds import recipe, styles
from common.meta import ART_SOURCE, INFER_MS, RAN_ON
from tests.conftest import TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}
TWO = ("akamai", "classic")


def a_pool(root: Path, size: int = 2, names: tuple[str, ...] = TWO, **key_parts) -> Pool:
    recipes = {style: recipe(style) for style in names}
    parts = {"model": "m", "revision": "r", "ran_on": "mock-gpu0", **key_parts}
    keys = {style: recipe_key(made, **parts) for style, made in recipes.items()}
    made = Pool(root, size, recipes, keys)
    made.load()
    return made


def png(colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buffer, "PNG")
    return buffer.getvalue()


def request_for(style: str, **changes) -> types.SimpleNamespace:
    return types.SimpleNamespace(**{**recipe(style), "seed": 1, **changes})


# ------------------------------------------------------------------ the files


def test_names_sort_in_the_order_the_backgrounds_were_painted() -> None:
    names = [entry_name(ns, seed=9, ms=100) for ns in (5, 40, 300, 2_000_000_000_000_000_000)]
    assert sorted(names) == names


def test_a_file_this_code_did_not_write_is_not_an_entry(tmp_path: Path) -> None:
    assert parse_entry(tmp_path / entry_name(1, 2, 3), "classic").seed == 2
    for stranger in ("notes.txt", "cat.png", "1-2.png", "a-b-c.png", "1-2-3.jpg"):
        assert parse_entry(tmp_path / stranger, "classic") is None


def test_the_recipe_key_covers_what_decides_the_picture_but_not_the_seed() -> None:
    base = recipe("classic")
    parts = {"model": "m", "revision": "r", "ran_on": "akamai-gpu0"}
    key = recipe_key(base, **parts)
    assert recipe_key({**base, "seed": 99}, **parts) == key
    changed = [
        recipe_key({**base, "prompt": base["prompt"] + " at dusk"}, **parts),
        recipe_key({**base, "width": 512}, **parts),
        recipe_key({**base, "height": 512}, **parts),
        recipe_key({**base, "steps": 8}, **parts),
        recipe_key(base, **{**parts, "model": "other"}),
        recipe_key(base, **{**parts, "revision": "other"}),
        # Seeds are reproducible on one card. Painted on another, it is another picture.
        recipe_key(base, **{**parts, "ran_on": "akamai-gpu1"}),
    ]
    assert key not in changed
    assert len(set(changed)) == len(changed)


def test_load_makes_a_directory_per_style(tmp_path: Path) -> None:
    made = a_pool(tmp_path / "pool")
    assert sorted(path.name.rsplit("-", 1)[0] for path in (tmp_path / "pool").iterdir()) == [
        "akamai",
        "classic",
    ]
    assert made.counts() == {"akamai": 0, "classic": 0}


def test_a_stale_recipe_is_dropped_and_a_strangers_file_is_left(tmp_path: Path) -> None:
    """styles.json or the model changed: the old pictures must not be served."""
    root = tmp_path / "pool"
    old = a_pool(root, model="the-old-model")
    stale = old.write("classic", png(), seed=3, ms=10)
    (stale.path.parent / "keep-me.txt").write_text("not ours")

    fresh = a_pool(root, model="the-new-model")

    assert fresh.ready("classic") == 0
    assert not stale.path.exists()
    assert (stale.path.parent / "keep-me.txt").exists()


def test_a_half_written_file_is_cleared_and_never_served(tmp_path: Path) -> None:
    root = tmp_path / "pool"
    first = a_pool(root)
    entry = first.write("classic", png(), seed=3, ms=10)
    part = entry.path.with_name("00000000000000000009-4-10.part")
    part.write_bytes(b"half a png")

    again = a_pool(root)

    assert not part.exists()
    assert [found.seed for found in again.index["classic"]] == [3]


def test_a_smaller_size_keeps_the_newest(tmp_path: Path) -> None:
    root = tmp_path / "pool"
    first = a_pool(root, size=3)
    for seed in (1, 2, 3):
        first.write("classic", png(), seed=seed, ms=10)

    smaller = a_pool(root, size=2)

    assert [entry.seed for entry in smaller.index["classic"]] == [2, 3]
    assert len(list(smaller.dirs["classic"].glob("*.png"))) == 2


def test_the_newest_is_taken_first(tmp_path: Path) -> None:
    made = a_pool(tmp_path)
    for seed in (11, 22):
        made.add(made.write("classic", png(), seed=seed, ms=10))

    assert made.take("classic").seed == 22
    assert made.take("classic").seed == 11
    assert made.take("classic") is None


def test_a_taken_background_leaves_the_disk(tmp_path: Path) -> None:
    """Served once. Left on disk it would be served again after a restart."""
    made = a_pool(tmp_path)
    made.add(made.write("classic", png((1, 2, 3)), seed=5, ms=10))

    entry = made.take("classic")
    data = Pool.read(entry)

    assert Image.open(io.BytesIO(data)).getpixel((0, 0)) == (1, 2, 3)
    assert not entry.path.exists()
    assert Pool.read(entry) is None


def test_the_style_with_the_fewest_ready_is_painted_next(tmp_path: Path) -> None:
    made = a_pool(tmp_path, size=2)
    assert made.wanted() == "akamai"  # a tie goes in styles.json order
    made.add(made.write("akamai", png(), seed=1, ms=1))
    assert made.wanted() == "classic"
    made.add(made.write("classic", png(), seed=2, ms=1))
    made.add(made.write("classic", png(), seed=3, ms=1))
    assert made.wanted() == "akamai"
    made.add(made.write("akamai", png(), seed=4, ms=1))
    assert made.wanted() is None
    assert (made.total(), made.target()) == (4, 4)


def test_only_a_styles_own_recipe_is_pooled(tmp_path: Path) -> None:
    """Someone calling /art/generate with a prompt of their own gets that prompt."""
    made = a_pool(tmp_path)
    assert made.matches(request_for("classic"))
    assert made.matches(request_for("classic", seed=12345))
    assert not made.matches(request_for("classic", prompt="a lighthouse at dusk"))
    assert not made.matches(request_for("classic", width=512))
    assert not made.matches(request_for("classic", height=512))
    assert not made.matches(request_for("classic", steps=8))
    # A style this pool does not keep, and one no pool keeps.
    only_classic = a_pool(tmp_path / "classic-only", names=("classic",))
    assert not only_classic.matches(request_for("akamai"))
    unknown = types.SimpleNamespace(**{**recipe("classic"), "seed": 1, "style": "no-such-style"})
    assert not made.matches(unknown)


# ---------------------------------------------------------------- the turns


async def test_a_request_waits_for_a_request() -> None:
    turns = Turns(asyncio.Lock())
    order: list[str] = []

    async def one(name: str, hold: float) -> None:
        async with turns.request():
            order.append(f"{name} in")
            await asyncio.sleep(hold)
            order.append(f"{name} out")

    await asyncio.gather(one("a", 0.05), one("b", 0))
    assert order == ["a in", "a out", "b in", "b out"]


async def test_the_model_is_busy_while_a_request_holds_it_or_waits_for_it() -> None:
    turns = Turns(asyncio.Lock())
    assert turns.idle_for() is not None
    inside = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with turns.request():
            inside.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await inside.wait()
    assert turns.idle_for() is None
    release.set()
    await task
    idle = turns.idle_for()
    assert idle is not None and idle < 0.5


async def test_a_request_that_arrives_mid_painting_tells_the_painting_to_give_way() -> None:
    turns = Turns(asyncio.Lock())
    async with turns.painting() as give_way:
        assert not give_way.is_set()
        waiting = asyncio.create_task(_enter(turns))
        await asyncio.sleep(0.01)
        assert give_way.is_set(), "the painter was not told a request is waiting"
        assert turns.idle_for() is None
    await waiting


async def test_a_new_painting_starts_with_the_flag_down() -> None:
    turns = Turns(asyncio.Lock())
    turns.give_way.set()
    async with turns.painting() as give_way:
        assert not give_way.is_set()


async def _enter(turns: Turns) -> None:
    async with turns.request():
        pass


# ---------------------------------------------------------------- the keeper


class Painter:
    """Stands in for service.paint. Records what it was asked, in which thread."""

    def __init__(self, fail: int = 0, give_way_once: bool = False, delay: float = 0.0) -> None:
        self.calls: list[types.SimpleNamespace] = []
        self.fail = fail
        self.give_way_once = give_way_once
        self.delay = delay

    def __call__(self, request, give_way: threading.Event):
        self.calls.append(request)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("CUDA out of memory")
        if self.give_way_once:
            self.give_way_once = False
            return None
        return Image.new("RGB", (8, 8), (len(self.calls), 0, 0))


def make_request(style: str, seed: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(**recipe(style), seed=seed)


@pytest.fixture
def quick(monkeypatch):
    """The keeper's waits, shortened so a test takes milliseconds."""
    monkeypatch.setattr(pool_module, "QUIET_S", 0.02)
    monkeypatch.setattr(pool_module, "POLL_S", 0.01)
    monkeypatch.setattr(pool_module, "BACKOFF_S", 0.02)


async def until(check, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_the_keeper_fills_every_style_then_waits(tmp_path: Path, quick) -> None:
    made = a_pool(tmp_path, size=2)
    painter = Painter()
    keeper = Keeper(made, Turns(asyncio.Lock()), painter, make_request)
    keeper.start()
    try:
        await until(lambda: made.total() == 4)
        await asyncio.sleep(0.1)
        assert len(painter.calls) == 4, "it kept painting a full pool"
    finally:
        await keeper.stop()

    # Each painting is a style's recipe with a seed of its own.
    assert [call.style for call in painter.calls] == [
        "akamai",
        "classic",
        "akamai",
        "classic",
    ]
    assert all(call.prompt == recipe(call.style)["prompt"] for call in painter.calls)
    assert len({call.seed for call in painter.calls}) == 4
    assert all(0 <= call.seed < 2**31 for call in painter.calls)
    # What the index says is what the disk holds.
    on_disk = {entry.seed for style in TWO for entry in made.index[style]}
    assert on_disk == {call.seed for call in painter.calls}


async def test_a_taken_background_is_painted_again(tmp_path: Path, quick) -> None:
    made = a_pool(tmp_path, size=1)
    keeper = Keeper(made, Turns(asyncio.Lock()), Painter(), make_request)
    keeper.start()
    try:
        await until(lambda: made.total() == 2)
        taken = made.take("classic")
        keeper.poke()
        await until(lambda: made.ready("classic") == 1)
        assert made.index["classic"][0].seed != taken.seed
    finally:
        await keeper.stop()


async def test_the_keeper_does_not_start_while_a_request_is_on_the_model(
    tmp_path: Path, quick
) -> None:
    made = a_pool(tmp_path, size=1)
    turns = Turns(asyncio.Lock())
    painter = Painter()
    keeper = Keeper(made, turns, painter, make_request)
    release = asyncio.Event()
    inside = asyncio.Event()

    async def a_card() -> None:
        async with turns.request():
            inside.set()
            await release.wait()

    card = asyncio.create_task(a_card())
    await inside.wait()
    keeper.start()
    try:
        await asyncio.sleep(0.1)
        assert painter.calls == [], "the painter went in while a card held the model"
        release.set()
        await card
        await until(lambda: made.total() == 2)
    finally:
        await keeper.stop()


async def test_requests_go_before_the_painter_and_it_waits_a_quiet_moment(
    tmp_path: Path, quick, monkeypatch
) -> None:
    """A card holds the model and a second card queues behind it. The painter,
    with a pool to fill, must not slip in between them: queued on the lock it
    would go first and paint a whole picture while the second card waited. And
    it starts no sooner than QUIET_S after the last card lets go."""
    monkeypatch.setattr(pool_module, "QUIET_S", 0.3)
    made = a_pool(tmp_path, size=1, names=("classic",))
    turns = Turns(asyncio.Lock())
    order: list[str] = []
    released: list[float] = []

    def painter(request, give_way):
        order.append("painter")
        released.append(time.monotonic())
        return Image.new("RGB", (8, 8))

    keeper = Keeper(made, turns, painter, make_request)
    first_in = asyncio.Event()
    let_go = asyncio.Event()

    async def first() -> None:
        async with turns.request():
            first_in.set()
            await let_go.wait()
        released.append(time.monotonic())

    async def second() -> None:
        async with turns.request():
            order.append("second card")
        released.append(time.monotonic())

    one = asyncio.create_task(first())
    await first_in.wait()
    keeper.start()
    try:
        await asyncio.sleep(0.05)
        two = asyncio.create_task(second())
        await asyncio.sleep(0.05)
        let_go.set()
        await asyncio.gather(one, two)
        await until(lambda: made.total() == 1)
    finally:
        await keeper.stop()

    assert order == ["second card", "painter"], order
    # released: first out, second out, then the painter's start.
    assert released[2] - released[1] >= 0.25, "the painter did not wait for a quiet moment"


async def test_a_painting_that_gave_way_is_thrown_out_and_painted_again(
    tmp_path: Path, quick
) -> None:
    made = a_pool(tmp_path, size=1, names=("classic",))
    painter = Painter(give_way_once=True)
    keeper = Keeper(made, Turns(asyncio.Lock()), painter, make_request)
    keeper.start()
    try:
        await until(lambda: made.total() == 1)
    finally:
        await keeper.stop()
    assert keeper.gave_way == 1
    assert len(painter.calls) == 2
    # The half-made one is nowhere: only the second painting is on disk.
    assert [entry.seed for entry in made.index["classic"]] == [painter.calls[1].seed]
    assert len(list(made.dirs["classic"].glob("*.png"))) == 1


async def test_a_painting_that_fails_is_tried_again_later(tmp_path: Path, quick) -> None:
    made = a_pool(tmp_path, size=1, names=("classic",))
    painter = Painter(fail=2)
    keeper = Keeper(made, Turns(asyncio.Lock()), painter, make_request)
    keeper.start()
    try:
        await until(lambda: keeper.last_error is not None)
        assert "out of memory" in keeper.last_error
        await until(lambda: made.total() == 1)
    finally:
        await keeper.stop()
    assert keeper.last_error is None
    assert len(painter.calls) == 3


async def test_a_request_that_comes_in_mid_painting_gets_the_model_next(
    tmp_path: Path, quick
) -> None:
    """The painter paints in a thread. A request waiting for the model sets the
    flag, and the painter lets go as soon as its painting returns."""
    made = a_pool(tmp_path, size=3, names=("classic",))
    turns = Turns(asyncio.Lock())
    seen: list[bool] = []

    def slow(request, give_way: threading.Event):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not give_way.is_set():
            time.sleep(0.005)
        seen.append(give_way.is_set())
        return None if give_way.is_set() else Image.new("RGB", (8, 8))

    keeper = Keeper(made, turns, slow, make_request)
    keeper.start()
    try:
        await until(lambda: turns._painting)
        started = time.monotonic()
        async with turns.request():
            waited = time.monotonic() - started
        assert waited < 0.5, f"the request waited {waited:.2f}s for a painting"
        assert seen[0] is True
    finally:
        await keeper.stop()


# ------------------------------------------------------------ the whole route


def pooled_service(monkeypatch):
    """art.main with the mock backend and one background per style.

    At a quarter of the size a card asks for, so the pool fills in a fraction
    of a second. recipe() reads the size when it is called, so the service and
    these tests agree on it.
    """
    from card import backgrounds
    from common.settings import get_settings

    monkeypatch.setattr(backgrounds, "ART_W", 256)
    monkeypatch.setattr(backgrounds, "ART_H", 352)
    monkeypatch.setenv("ART_POOL_SIZE", "1")
    monkeypatch.setenv("GPU_LABEL", "mock-gpu0")
    get_settings.cache_clear()
    return importlib.reload(importlib.import_module("art.main"))


@pytest.fixture
def art_pooled(clean_settings, monkeypatch, quick) -> TestClient:
    with TestClient(pooled_service(monkeypatch).app) as client:
        yield client


def health(client: TestClient) -> dict:
    return client.get("/healthz", headers=AUTH).json()


def fill(client: TestClient) -> dict:
    deadline = time.monotonic() + 30
    while True:
        body = health(client)
        if body["pool_ready"] == body["pool_size"]:
            return body
        assert time.monotonic() < deadline, f"the pool did not fill: {body['pool_ready']}"
        time.sleep(0.05)


def generate(client: TestClient, style: str = "classic", **changes):
    return client.post("/art/generate", headers=AUTH, json={**recipe(style), "seed": 5, **changes})


def test_healthz_says_how_full_the_pool_is(art_pooled: TestClient) -> None:
    body = fill(art_pooled)
    assert body["pool_size"] == len(styles())
    assert body["pool_ready"] == len(styles())
    assert body["ran_on"] == "mock-gpu0"


def test_a_card_gets_a_pooled_background_at_once(art_pooled: TestClient) -> None:
    fill(art_pooled)
    started = time.perf_counter()
    response = generate(art_pooled)
    took = time.perf_counter() - started

    assert response.status_code == 200
    assert response.headers[ART_SOURCE] == "pool"
    assert response.headers[RAN_ON] == "mock-gpu0"
    assert int(response.headers[INFER_MS]) > 0, "X-Infer-Ms is how long the painting took"
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (recipe("classic")["width"], recipe("classic")["height"])
    assert took < 1.0
    # Painted before the card asked, with a seed of the pool's own.
    assert response.content != generate(art_pooled, fresh=True).content


def test_with_none_ready_the_background_is_painted_with_the_cards_seed(
    art_pooled: TestClient,
) -> None:
    fill(art_pooled)
    generate(art_pooled)
    response = generate(art_pooled)

    assert response.headers[ART_SOURCE] == "fresh"
    # The mock is deterministic in the seed, so the card's seed draws this one.
    assert response.content == generate(art_pooled, fresh=True).content


def test_fresh_paints_even_with_one_ready_and_leaves_the_pool_alone(
    art_pooled: TestClient,
) -> None:
    fill(art_pooled)
    first = generate(art_pooled, fresh=True, seed=77)
    again = generate(art_pooled, fresh=True, seed=77)

    assert first.headers[ART_SOURCE] == again.headers[ART_SOURCE] == "fresh"
    assert first.content == again.content
    body = health(art_pooled)
    assert body["pool_ready"] == body["pool_size"]


def test_a_prompt_of_your_own_is_painted_even_with_the_style_named(
    art_pooled: TestClient,
) -> None:
    """examples/curl.md sends a style with its own prompt. It gets its prompt."""
    fill(art_pooled)
    response = generate(art_pooled, prompt="a lighthouse at dusk, no text")

    assert response.headers[ART_SOURCE] == "fresh"
    body = health(art_pooled)
    assert body["pool_ready"] == body["pool_size"]


def test_the_pool_fills_again_after_a_card_takes_one(art_pooled: TestClient) -> None:
    fill(art_pooled)
    first = generate(art_pooled)
    assert first.headers[ART_SOURCE] == "pool"
    fill(art_pooled)
    second = generate(art_pooled)
    assert second.headers[ART_SOURCE] == "pool"
    assert second.content != first.content


def test_two_cards_never_get_the_same_background(art_pooled: TestClient) -> None:
    from art import main

    fill(art_pooled)
    main.keeper.pool.size = 3
    # On the service's event loop: an asyncio.Event set from this thread would
    # not wake it.
    art_pooled.portal.call(main.keeper.poke)
    fill(art_pooled)
    answers = [generate(art_pooled) for _ in range(3)]
    assert {answer.headers[ART_SOURCE] for answer in answers} == {"pool"}
    assert len({answer.content for answer in answers}) == 3


def test_a_pool_that_is_off_says_so(clean_settings) -> None:
    """ART_POOL_SIZE=0, which is what clean_settings sets: every card is painted."""
    module = importlib.reload(importlib.import_module("art.main"))
    with TestClient(module.app) as client:
        body = client.get("/healthz", headers=AUTH).json()
        assert (body["pool_ready"], body["pool_size"]) == (0, 0)
        response = client.post("/art/generate", headers=AUTH, json={**recipe("classic"), "seed": 5})
        assert response.headers[ART_SOURCE] == "fresh"
        assert module.keeper is None


def test_a_pool_directory_that_cannot_be_made_leaves_the_service_up(
    clean_settings, monkeypatch, tmp_path: Path
) -> None:
    """The pool is a nicety. A card still gets a background, painted when it asks."""
    from common.settings import get_settings

    blocked = tmp_path / "a-file"
    blocked.write_text("in the way")
    monkeypatch.setenv("ART_POOL_SIZE", "1")
    monkeypatch.setenv("DATA_DIR", str(blocked))
    get_settings.cache_clear()
    module = importlib.reload(importlib.import_module("art.main"))
    with TestClient(module.app) as client:
        assert client.get("/healthz", headers=AUTH).json()["loaded"] is True
        response = client.post("/art/generate", headers=AUTH, json={**recipe("classic"), "seed": 5})
        assert response.status_code == 200
        assert response.headers[ART_SOURCE] == "fresh"


def test_the_painter_stops_with_the_service(clean_settings, monkeypatch, quick) -> None:
    """on_stop: a painter left running would hold the model past the end."""
    module = pooled_service(monkeypatch)
    with TestClient(module.app):
        task = module.keeper.task
        assert task is not None and not task.done()
    assert task.done()
