"""The pipeline runner.

The point of these is the failure cases. Any one of cutout, art, or text can go
down and a card still has to come out, because a demo that shows nothing when a
GPU hiccups is worse than a demo with a plain background.
"""

from __future__ import annotations

import io
import json
import threading

import httpx
import pytest
from PIL import Image

from api import pipeline as pipeline_module
from api.db import Database
from api.pipeline import ASSET_NAMES, STEPS, Pipeline
from api.storage import Storage
from card.compose import PICTURE
from common.meta import ART_SOURCE, model_headers
from common.schemas import Stats

STATS = Stats(name="Sam Rivera", position="SS", metrics={"exit_velo_mph": 88.5})
STYLE = "akamai"
CARD_ID = "test-card-0001"


def png(size: tuple[int, int], mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, (30, 60, 90) if mode == "RGB" else (30, 60, 90, 255)).save(buffer, "PNG")
    return buffer.getvalue()


def chat(content: dict) -> httpx.Response:
    body = {"model": "mock/wordlist", "choices": [{"message": {"content": json.dumps(content)}}]}
    return httpx.Response(200, json=body)


# Passes api.text.check for STATS: three fields, one of its numbers, no banned word.
TEXT_REPLY = chat(
    {
        "nickname": "Turnstile",
        "tagline": "Quick hands, short path",
        "blurb": "The ball leaves the bat at 88.5 mph. Gap to gap.",
    }
)


def routes(**answers: httpx.Response) -> httpx.MockTransport:
    """Answer /cutout, /art/generate and /v1/chat/completions with what a test says."""

    def handler(request: httpx.Request) -> httpx.Response:
        for key, path in (
            ("cutout", "/cutout"),
            ("art", "/art/generate"),
            ("text", "/v1/chat/completions"),
        ):
            if request.url.path == path:
                answer = answers.get(key)
                if answer is None:
                    raise httpx.ConnectError("connection refused", request=request)
                return answer
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def parts(clean_settings, tmp_path, monkeypatch):
    """A pipeline wired to a throwaway volume, with every service configured."""
    monkeypatch.setenv("CUTOUT_URL", "http://cutout:8001")
    monkeypatch.setenv("ART_URL", "http://art:8002")
    monkeypatch.setenv("LLM_URL", "http://llm:8000")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("LLM_LICENSE", "Apache-2.0")
    monkeypatch.setenv("LLM_RAN_ON", "akamai-gpu1")
    from common.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    storage = Storage(settings.data_dir)
    storage.prepare()
    db = Database(storage.db_path)
    db.init()
    db.create_card(CARD_ID, STATS, STYLE, STEPS)
    storage.write_photo(CARD_ID, png((100, 140)))
    return Pipeline(CARD_ID, STATS, STYLE, settings, db, storage), db, storage


def step_named(db: Database, name: str):
    return next(step for step in db.get_job(CARD_ID).steps if step.step == name)


def stub_render(monkeypatch, storage: Storage):
    """Stand in for Chromium. The real render is exercised in tests/integration."""
    calls: list[dict] = []

    def fake(stats, *, style, art, cutout, out_dir, text, comps, steps, names, **placed):
        calls.append(
            {
                "stats": stats,
                "style": style,
                "art": art,
                "cutout": cutout,
                "steps": list(steps),
                **placed,
            }
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        for side in ASSET_NAMES:
            path = out_dir / names[side]
            path.write_bytes(b"rendered " + side.encode())
            written[side] = path
        return written

    monkeypatch.setattr(pipeline_module, "render_card", fake)
    return calls


def use(monkeypatch, transport: httpx.MockTransport) -> None:
    """Point the pipeline's httpx client at a transport the test controls."""
    real = httpx.AsyncClient

    def build(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", build)


# -- one branch at a time ----------------------------------------------------


async def test_cutout_records_what_the_service_reported(parts, monkeypatch) -> None:
    pipeline, db, _ = parts
    headers = model_headers("ZhengPeng7/BiRefNet", "MIT", 412, "akamai-gpu0")
    transport = routes(cutout=httpx.Response(200, content=png((80, 100), "RGBA"), headers=headers))
    async with httpx.AsyncClient(transport=transport) as client:
        image = await pipeline.cutout(client, png((100, 140)))

    assert image is not None and image.mode == "RGBA"
    step = step_named(db, "cutout")
    assert (step.status, step.model, step.license, step.ran_on, step.ms) == (
        "done",
        "ZhengPeng7/BiRefNet",
        "MIT",
        "akamai-gpu0",
        412,
    )


async def test_a_failed_cutout_leaves_the_card_with_just_the_background(parts) -> None:
    pipeline, db, _ = parts
    transport = routes(cutout=httpx.Response(500, text="out of memory"))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await pipeline.cutout(client, png((100, 140))) is None
    step = step_named(db, "cutout")
    assert step.status == "failed"
    assert "500" in step.error


async def test_a_cutout_service_that_is_not_configured_is_skipped(parts, monkeypatch) -> None:
    pipeline, db, _ = parts
    monkeypatch.setattr(pipeline.settings, "cutout_url", "")
    async with httpx.AsyncClient(transport=routes()) as client:
        assert await pipeline.cutout(client, png((100, 140))) is None
    assert step_named(db, "cutout").status == "skipped"


async def test_art_records_what_the_service_reported(parts) -> None:
    pipeline, db, _ = parts
    headers = model_headers("black-forest-labs/FLUX.2-klein-4B", "Apache-2.0", 2180, "akamai-gpu0")
    transport = routes(art=httpx.Response(200, content=png((64, 88)), headers=headers))
    async with httpx.AsyncClient(transport=transport) as client:
        image = await pipeline.art(client)
    assert image.size == (64, 88)
    step = step_named(db, "art")
    assert (step.status, step.ran_on, step.ms) == ("done", "akamai-gpu0", 2180)


async def test_art_that_fails_falls_back_and_says_why(parts) -> None:
    pipeline, db, _ = parts
    async with httpx.AsyncClient(transport=routes(art=httpx.Response(503))) as client:
        image = await pipeline.art(client)

    assert image.size == (1024, 1408)
    step = step_named(db, "art")
    # Done, because the card has a background. The error says it is not the one
    # the model would have painted.
    assert step.status == "done"
    assert step.model == "procedural"
    assert "503" in step.error


FLUX_ID = "black-forest-labs/FLUX.2-klein-4B"


def background(source: str | None, infer_ms: int = 4500) -> httpx.Response:
    headers = model_headers(FLUX_ID, "Apache-2.0", infer_ms, "akamai-gpu0")
    if source is not None:
        headers[ART_SOURCE] = source
    return httpx.Response(200, content=png((64, 88)), headers=headers)


async def test_a_pooled_background_is_timed_by_what_the_card_waited(parts) -> None:
    """X-Infer-Ms on a pooled one is how long it took to paint, before the card
    asked. The row is what this card spent on the step, and says pool."""
    pipeline, db, _ = parts
    async with httpx.AsyncClient(transport=routes(art=background("pool"))) as client:
        await pipeline.art(client)

    step = step_named(db, "art")
    assert (step.status, step.model, step.source, step.ran_on) == (
        "done",
        FLUX_ID,
        "pool",
        "akamai-gpu0",
    )
    assert step.ms < 4500


async def test_a_fresh_background_is_timed_by_its_painting(parts) -> None:
    pipeline, db, _ = parts
    async with httpx.AsyncClient(transport=routes(art=background("fresh"))) as client:
        await pipeline.art(client)

    step = step_named(db, "art")
    assert (step.source, step.ms) == ("fresh", 4500)


@pytest.mark.parametrize("source", [None, "banana"])
async def test_an_art_service_that_does_not_say_where_it_came_from_leaves_it_blank(
    parts, source
) -> None:
    """An art service that says nothing about where a background came from, or
    says something else, gets nothing guessed for it."""
    pipeline, db, _ = parts
    async with httpx.AsyncClient(transport=routes(art=background(source))) as client:
        await pipeline.art(client)

    step = step_named(db, "art")
    assert (step.status, step.source, step.ms) == ("done", None, 4500)


async def test_a_background_that_fell_back_is_neither_pool_nor_fresh(parts) -> None:
    pipeline, db, _ = parts
    async with httpx.AsyncClient(transport=routes(art=httpx.Response(503))) as client:
        await pipeline.art(client)
    step = step_named(db, "art")
    assert (step.model, step.source) == ("procedural", None)


async def test_the_card_asks_for_its_styles_recipe_and_not_for_a_fresh_one(parts) -> None:
    from card.backgrounds import recipe

    pipeline, _, _ = parts
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return background("pool")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await pipeline.art(client)

    body = seen[0]
    assert {key: body[key] for key in recipe(STYLE)} == recipe(STYLE)
    assert body["fresh"] is False


async def test_comps_records_its_own_row_and_reports_the_host(parts) -> None:
    pipeline, db, _ = parts
    found = await pipeline.comps()

    assert len(found) == 3
    step = step_named(db, "comps")
    assert (step.status, step.model, step.license, step.error) == (
        "done",
        pipeline_module.COMPS_MODEL,
        "n/a",
        None,
    )
    # numpy on whatever box the api is on. Never a GPU label from a model service.
    assert step.ran_on == pipeline.settings.gpu_label
    assert step.source is None


async def test_comps_is_skipped_when_no_metrics_were_submitted(parts) -> None:
    pipeline, db, _ = parts
    pipeline.stats = STATS.model_copy(update={"metrics": {}})

    assert await pipeline.comps() == []
    step = step_named(db, "comps")
    assert (step.status, step.model) == ("skipped", None)
    assert "no metrics" in step.error


async def test_comps_is_skipped_when_nothing_submitted_is_in_the_table(parts) -> None:
    pipeline, db, _ = parts
    # A real stats.json key that Baseball Savant does not publish.
    pipeline.stats = STATS.model_copy(update={"metrics": {"time_to_contact_ms": 155.0}})

    assert await pipeline.comps() == []
    assert step_named(db, "comps").status == "skipped"


async def test_comps_count_0_turns_the_section_off(parts, monkeypatch) -> None:
    pipeline, db, _ = parts
    monkeypatch.setattr(pipeline.settings, "comps_count", 0)

    assert await pipeline.comps() == []
    step = step_named(db, "comps")
    assert (step.status, step.error) == ("skipped", "COMPS_COUNT is 0")


async def test_a_missing_comps_table_does_not_stop_the_card(parts, monkeypatch) -> None:
    pipeline, db, _ = parts

    def gone(*args):
        raise FileNotFoundError(2, "No such file or directory", "statcast_batters.csv")

    monkeypatch.setattr(pipeline_module, "lookup", gone)

    assert await pipeline.comps() == []
    step = step_named(db, "comps")
    assert step.status == "failed"
    assert "FileNotFoundError" in step.error


async def test_the_llm_is_not_told_who_the_comps_are(parts) -> None:
    """The "Swings like" table says it. A model that is not told a real hitter's
    name has nothing to say about one."""
    pipeline, db, _ = parts
    comps = await pipeline.comps()
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return TEXT_REPLY

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await pipeline.text(client)

    prompt = json.dumps(sent[0]["messages"])
    assert len(comps) == 3
    for comp in comps:
        assert comp.player not in prompt
    assert "closest" not in prompt.lower()


async def test_text_does_not_wait_for_the_comps(parts) -> None:
    """Four branches side by side. The text no longer runs the lookup first."""
    pipeline, db, _ = parts
    async with httpx.AsyncClient(transport=routes(text=TEXT_REPLY)) as client:
        await pipeline.text(client)

    assert step_named(db, "text").status == "done"
    assert step_named(db, "comps").status == "queued"


async def test_text_uses_the_settings_for_what_vllm_does_not_report(parts) -> None:
    pipeline, db, storage = parts
    async with httpx.AsyncClient(transport=routes(text=TEXT_REPLY)) as client:
        written = await pipeline.text(client)

    assert written.nickname == "Turnstile"
    step = step_named(db, "text")
    assert (step.status, step.model, step.license, step.ran_on) == (
        "done",
        "mock/wordlist",
        "Apache-2.0",
        "akamai-gpu1",
    )
    # The one try is kept beside the other intermediates, with nothing wrong in it.
    tries = json.loads((storage.work_dir(CARD_ID) / "text.json").read_text())
    assert [attempt["problems"] for attempt in tries] == [[]]


async def test_text_that_fails_falls_back_to_the_template(parts) -> None:
    pipeline, db, _ = parts
    async with httpx.AsyncClient(transport=routes(text=httpx.Response(500))) as client:
        written = await pipeline.text(client)

    assert written.blurb == "The ball leaves at 88.5 mph."
    assert written.nickname == "Line Drive"
    assert step_named(db, "text").status == "failed"


async def test_text_that_fails_its_checks_twice_gets_the_template(parts) -> None:
    """The model answered, twice, and neither answer could go on a card. The step
    says failed, so the ledger on the back does not credit the model with words it
    did not write, and both answers are kept for whoever reads the prompt next."""
    pipeline, db, storage = parts
    praised = chat(
        {
            "nickname": "Rivera",
            "tagline": "A solid bat",
            "blurb": "Sam shows great mechanics at 88.5 mph.",
        }
    )
    async with httpx.AsyncClient(transport=routes(text=praised)) as client:
        written = await pipeline.text(client)

    assert written.blurb == "The ball leaves at 88.5 mph."
    step = step_named(db, "text")
    assert step.status == "failed"
    assert step.error.startswith("the text failed its checks.")
    assert step.model is None
    # The reason reaches the log and the app, so it carries no word of the name.
    assert "Rivera" not in step.error
    tries = json.loads((storage.work_dir(CARD_ID) / "text.json").read_text())
    assert len(tries) == 2
    assert all(attempt["problems"] for attempt in tries)
    assert "Rivera" in tries[0]["answer"]


# -- the whole job -----------------------------------------------------------


async def test_a_good_run_finishes_and_names_the_files(parts, monkeypatch) -> None:
    pipeline, db, storage = parts
    calls = stub_render(monkeypatch, storage)
    use(
        monkeypatch,
        routes(
            cutout=httpx.Response(
                200,
                content=png((80, 100), "RGBA"),
                headers=model_headers("BiRefNet", "MIT", 400, "akamai-gpu0"),
            ),
            art=httpx.Response(
                200,
                content=png((64, 88)),
                headers=model_headers("FLUX", "Apache-2.0", 2000, "akamai-gpu0"),
            ),
            text=TEXT_REPLY,
        ),
    )

    await pipeline.run()

    job = db.get_job(CARD_ID)
    assert job.status == "done"
    assert {step.step for step in job.steps} == set(STEPS)
    assert all(step.status == "done" for step in job.steps)
    assert job.text.nickname == "Turnstile"
    for name in ASSET_NAMES.values():
        assert storage.asset_path(CARD_ID, name).is_file()
    # The renderer is handed the composited hero, not the raw art and cutout,
    # at the size of the card's picture, and the style the card was posted in.
    assert calls[0]["cutout"] is None
    assert calls[0]["art"].size == PICTURE
    assert calls[0]["style"] == STYLE


async def test_the_ledger_on_the_card_only_has_the_model_steps(parts, monkeypatch) -> None:
    pipeline, db, storage = parts
    calls = stub_render(monkeypatch, storage)
    use(monkeypatch, routes(art=httpx.Response(200, content=png((64, 88))), text=TEXT_REPLY))
    await pipeline.run()

    on_card = [step.step for step in calls[0]["steps"] if step.model and step.status == "done"]
    assert "compose" not in on_card
    assert "render" not in on_card


async def test_a_card_still_comes_out_when_every_service_is_down(parts, monkeypatch) -> None:
    """The whole point of the fallbacks. No cutout, no art, no text, still a card."""
    pipeline, db, storage = parts
    stub_render(monkeypatch, storage)
    use(monkeypatch, routes())

    await pipeline.run()

    job = db.get_job(CARD_ID)
    assert job.status == "done"
    assert job.text.blurb == "The ball leaves at 88.5 mph."
    assert storage.asset_path(CARD_ID, "front.png").is_file()
    assert step_named(db, "cutout").status == "failed"
    assert step_named(db, "art").status == "done"
    assert step_named(db, "text").status == "failed"


async def test_a_job_whose_upload_vanished_fails_cleanly(parts, monkeypatch) -> None:
    pipeline, db, storage = parts
    storage.photo_path(CARD_ID).unlink()
    await pipeline.run()
    job = db.get_job(CARD_ID)
    assert job.status == "failed"
    assert "upload is gone" in job.error


async def test_a_render_that_blows_up_fails_the_job_and_says_so(parts, monkeypatch) -> None:
    pipeline, db, _ = parts

    def explode(*args, **kwargs):
        raise RuntimeError("chromium did not start")

    monkeypatch.setattr(pipeline_module, "render_card", explode)
    use(monkeypatch, routes(art=httpx.Response(200, content=png((64, 88))), text=TEXT_REPLY))

    await pipeline.run()

    job = db.get_job(CARD_ID)
    assert job.status == "failed"
    assert "chromium did not start" in job.error


async def test_a_render_that_fails_after_the_front_leaves_no_files(parts, monkeypatch) -> None:
    """The front and its preview are written before the back. If the back then
    gives out, the card fails and keeps none of them."""
    pipeline, db, storage = parts

    def half(stats, *, out_dir, names, **rest):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / names["front"]).write_bytes(b"front")
        (out_dir / names["preview"]).write_bytes(b"preview")
        raise RuntimeError("the back page crashed")

    monkeypatch.setattr(pipeline_module, "render_card", half)
    use(monkeypatch, routes(art=httpx.Response(200, content=png((64, 88))), text=TEXT_REPLY))

    await pipeline.run()

    job = db.get_job(CARD_ID)
    assert job.status == "failed"
    assert "the back page crashed" in job.error
    assert storage.ready(CARD_ID) == []


async def test_the_face_check_does_not_wait_for_the_render(parts, monkeypatch) -> None:
    """Where the face landed is read while the card renders. The check here
    waits for the render to start: run one after the other, it never would."""
    pipeline, db, storage = parts
    rendering = threading.Event()
    seen: dict[str, bool] = {}

    def check_face(cutout, face, sent):
        seen["the render had started"] = rendering.wait(5)
        return ["a warning from the check"]

    def render(stats, *, out_dir, names, **rest):
        rendering.set()
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in names.values():
            (out_dir / name).write_bytes(b"rendered")

    monkeypatch.setattr(pipeline_module.qa, "check_face", check_face)
    monkeypatch.setattr(pipeline_module, "render_card", render)
    use(
        monkeypatch,
        routes(
            cutout=httpx.Response(200, content=png((80, 100), "RGBA")),
            art=httpx.Response(200, content=png((64, 88))),
            text=TEXT_REPLY,
        ),
    )

    await pipeline.run()

    assert seen == {"the render had started": True}
    job = db.get_job(CARD_ID)
    assert job.status == "done"
    assert "a warning from the check" in job.warnings


async def test_the_intermediates_are_kept_for_debugging(parts, monkeypatch) -> None:
    pipeline, db, storage = parts
    stub_render(monkeypatch, storage)
    use(
        monkeypatch,
        routes(
            cutout=httpx.Response(200, content=png((80, 100), "RGBA")),
            art=httpx.Response(200, content=png((64, 88))),
            text=TEXT_REPLY,
        ),
    )
    await pipeline.run()
    assert (storage.work_dir(CARD_ID) / "cutout.png").is_file()
    assert (storage.work_dir(CARD_ID) / "art.png").is_file()


# -- photo qa ----------------------------------------------------------------


def framed_cutout(size: tuple[int, int] = (400, 600)) -> bytes:
    """A cutout of someone standing where they should be. No warnings in it."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    width, height = size
    box = (int(width * 0.3), int(height * 0.1), int(width * 0.7), height)
    image.paste((200, 150, 120, 255), box)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


async def test_a_well_framed_photo_has_no_warnings(parts) -> None:
    pipeline, db, _ = parts
    transport = routes(cutout=httpx.Response(200, content=framed_cutout()))
    async with httpx.AsyncClient(transport=transport) as client:
        await pipeline.cutout(client, png((100, 140)))
    assert db.get_job(CARD_ID).warnings == []


async def test_a_photo_cut_off_at_the_edges_says_so(parts) -> None:
    """An all-opaque matte fills the frame, so it is cut off on three sides."""
    pipeline, db, _ = parts
    transport = routes(cutout=httpx.Response(200, content=png((400, 600), "RGBA")))
    async with httpx.AsyncClient(transport=transport) as client:
        await pipeline.cutout(client, png((100, 140)))
    assert db.get_job(CARD_ID).warnings == [
        "cut off at the left",
        "cut off at the right",
        "cut off at the top",
    ]
    # And the step itself is still a success. A warning is not a failure.
    assert step_named(db, "cutout").status == "done"


async def test_an_empty_matte_says_no_subject_was_found(parts) -> None:
    pipeline, db, _ = parts
    empty = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
    buffer = io.BytesIO()
    empty.save(buffer, "PNG")
    transport = routes(cutout=httpx.Response(200, content=buffer.getvalue()))
    async with httpx.AsyncClient(transport=transport) as client:
        await pipeline.cutout(client, png((100, 140)))
    assert db.get_job(CARD_ID).warnings == ["no subject found"]


async def test_a_broken_qa_check_never_costs_a_card(parts, monkeypatch) -> None:
    pipeline, db, _ = parts

    def explode(_image):
        raise RuntimeError("numpy fell over")

    monkeypatch.setattr("api.qa.check_cutout", explode)
    transport = routes(cutout=httpx.Response(200, content=framed_cutout()))
    async with httpx.AsyncClient(transport=transport) as client:
        image = await pipeline.cutout(client, png((100, 140)))

    assert image is not None
    assert step_named(db, "cutout").status == "done"
    assert db.get_job(CARD_ID).warnings == []


# -- the face box ------------------------------------------------------------
#
# framed_cutout() puts the person at (120, 60) to (280, 600) in a 400 x 600
# matte. The card's picture is 1692 px tall. The rule makes a face 0.14 of it,
# 236.9 px, centred 0.27 down, and stands the person on the bottom edge. A face
# 110 px tall has 475 px of person under its centre, so it grows from 2.15
# times to 2.60 to reach the edge, 0.169 of the picture, inside the bands. One
# 54 px tall would need 4.4 times, stops at 3, and is 162 px there, under the
# 10 percent line.

FACE = (160, 70, 80, 110)
SMALL = (172, 70, 54, 54)
OFF = (0, 0, 60, 40)  # in the matte's corner, nowhere near the person


def every_service_up(monkeypatch, cutout: bytes | None = None) -> None:
    use(
        monkeypatch,
        routes(
            cutout=httpx.Response(200, content=cutout or framed_cutout()),
            art=httpx.Response(200, content=png((64, 88))),
            text=TEXT_REPLY,
        ),
    )


def spy_compose(monkeypatch, db: Database) -> list[dict]:
    """Record what compose_hero was handed, and the job's warnings at that moment."""
    seen: list[dict] = []
    real = pipeline_module.compose_hero

    def spy(*args, **kwargs):
        seen.append({"kwargs": kwargs, "warnings": db.get_job(CARD_ID).warnings})
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "compose_hero", spy)
    return seen


async def test_a_face_box_goes_to_compose_and_not_to_the_render(parts, monkeypatch) -> None:
    """The face places the person, and compose is where that happens. The
    render gets the picture compose made."""
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, FACE)
    calls = stub_render(monkeypatch, storage)
    seen = spy_compose(monkeypatch, db)
    every_service_up(monkeypatch)

    await pipeline.run()

    job = db.get_job(CARD_ID)
    assert job.status == "done"
    assert seen[0]["kwargs"] == {"face": FACE}
    assert "face" not in calls[0]
    assert job.warnings == []


async def test_a_null_face_gets_the_no_face_warning_after_compose(parts, monkeypatch) -> None:
    """The browser looked and found nobody, and the job says so once the card
    is composed."""
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, None)
    calls = stub_render(monkeypatch, storage)
    seen = spy_compose(monkeypatch, db)
    every_service_up(monkeypatch)

    await pipeline.run()

    assert seen[0] == {"kwargs": {"face": None}, "warnings": []}
    assert db.get_job(CARD_ID).warnings == ["no face found"]
    assert db.get_job(CARD_ID).status == "done"
    assert "face" not in calls[0]


async def test_a_box_that_is_not_on_the_person_gets_the_no_face_warning(parts, monkeypatch) -> None:
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, OFF)
    stub_render(monkeypatch, storage)
    every_service_up(monkeypatch)

    await pipeline.run()

    assert db.get_job(CARD_ID).warnings == ["no face found"]


async def test_a_face_the_cap_leaves_small_says_so(parts, monkeypatch) -> None:
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, SMALL)
    stub_render(monkeypatch, storage)
    every_service_up(monkeypatch)

    await pipeline.run()

    assert db.get_job(CARD_ID).warnings == ["face small"]


async def test_no_face_file_means_no_face_found_is_never_said(parts, monkeypatch) -> None:
    """cardctl sends no face field. The top of the silhouette places the person,
    and nothing on the job complains about a box nobody sent."""
    pipeline, db, storage = parts
    calls = stub_render(monkeypatch, storage)
    seen = spy_compose(monkeypatch, db)
    every_service_up(monkeypatch)

    await pipeline.run()

    assert seen[0]["kwargs"] == {"face": None}
    assert "face" not in calls[0]
    assert db.get_job(CARD_ID).warnings == []


async def test_a_card_with_no_face_field_still_says_where_the_person_landed(
    parts, monkeypatch
) -> None:
    """cardctl's cards are checked too. A matte that fades out under the feet:
    270 px of person over 120 px at alpha 100. Compose stands the faint strip
    on the bottom edge, so the person ends 0.76 of the way down, and the strip
    counts as body, so the top 15 percent of the silhouette, the face, grows
    only to 0.08. The job says both."""
    pipeline, db, storage = parts
    stub_render(monkeypatch, storage)
    soft = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
    soft.paste((200, 150, 120, 255), (120, 60, 280, 330))
    soft.paste((200, 150, 120, 100), (120, 330, 280, 450))
    buffer = io.BytesIO()
    soft.save(buffer, "PNG")
    every_service_up(monkeypatch, cutout=buffer.getvalue())

    await pipeline.run()

    assert db.get_job(CARD_ID).warnings == ["face small", "floats"]


BUST_FACE = (125, 60, 150, 150)


async def test_face_warnings_come_after_the_photo_qa_ones(parts, monkeypatch) -> None:
    """An all-opaque matte is cut off on three sides. Photo QA writes that first,
    and the face check adds to it rather than writing over it."""
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, None)
    stub_render(monkeypatch, storage)
    every_service_up(monkeypatch, cutout=png((400, 600), "RGBA"))

    await pipeline.run()

    assert db.get_job(CARD_ID).warnings == [
        "cut off at the left",
        "cut off at the right",
        "cut off at the top",
        "no face found",
    ]


@pytest.mark.parametrize(
    ("face", "said"),
    [(FACE, []), (None, ["no face found"])],
)
async def test_a_failed_cutout_only_repeats_what_the_browser_found(
    parts, monkeypatch, face, said
) -> None:
    """Nobody is on the card. A box has nobody to have placed, so it says
    nothing. A null is the browser finding no face, and that still goes on."""
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, face)
    calls = stub_render(monkeypatch, storage)
    use(
        monkeypatch,
        routes(
            cutout=httpx.Response(500, text="out of memory"),
            art=httpx.Response(200, content=png((64, 88))),
            text=TEXT_REPLY,
        ),
    )

    await pipeline.run()

    job = db.get_job(CARD_ID)
    assert job.status == "done"
    assert job.warnings == said
    assert "face" not in calls[0]


@pytest.mark.parametrize(
    ("face", "said"),
    [
        (FACE, ["no subject found"]),
        (None, ["no subject found", "no face found"]),
    ],
)
async def test_an_empty_matte_only_repeats_what_the_browser_found(
    parts, monkeypatch, face, said
) -> None:
    """A flat grey photo in the app: the matte finds nobody and the browser finds
    no face, and the card says both."""
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, face)
    stub_render(monkeypatch, storage)
    empty = io.BytesIO()
    Image.new("RGBA", (400, 600), (0, 0, 0, 0)).save(empty, "PNG")
    every_service_up(monkeypatch, cutout=empty.getvalue())

    await pipeline.run()

    assert db.get_job(CARD_ID).warnings == said


async def test_a_broken_face_check_never_costs_a_card(parts, monkeypatch) -> None:
    pipeline, db, storage = parts
    storage.write_face(CARD_ID, None)
    stub_render(monkeypatch, storage)
    every_service_up(monkeypatch)

    def explode(*_args):
        raise RuntimeError("numpy fell over")

    monkeypatch.setattr("api.qa.check_face", explode)
    await pipeline.run()

    job = db.get_job(CARD_ID)
    assert job.status == "done"
    assert job.warnings == []
