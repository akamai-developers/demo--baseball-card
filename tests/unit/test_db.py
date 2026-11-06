"""The job database. One SQLite file, two tables, no ORM."""

from __future__ import annotations

import json
import time

import pytest

from api.db import Database
from common.schemas import CardText, Comp, Stats


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "cards.db")
    database.init()
    return database


@pytest.fixture
def stats() -> Stats:
    return Stats(name="Sam Rivera", number=23, position="SS", metrics={"bat_speed_mph": 68.2})


def test_init_is_safe_to_run_twice(tmp_path) -> None:
    database = Database(tmp_path / "nested" / "cards.db")
    database.init()
    database.init()
    assert database.path.is_file()


def test_wal_mode_is_on(db: Database) -> None:
    """Readers have to keep working while a pipeline branch is writing."""
    with db.connect() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_a_new_card_is_queued_with_queued_steps(db: Database, stats: Stats) -> None:
    db.create_card("abc", stats, "akamai", ("cutout", "art", "text"))
    job = db.get_job("abc")
    assert job is not None
    assert job.status == "queued"
    assert [step.step for step in job.steps] == ["cutout", "art", "text"]
    assert {step.status for step in job.steps} == {"queued"}


def test_a_card_keeps_what_it_was_made_from(db: Database, stats: Stats) -> None:
    """The stats and the style, for whoever opens cards.db to see why a card
    came out as it did."""
    db.create_card("abc", stats, "classic", ())
    with db.connect() as connection:
        row = connection.execute("SELECT stats, style FROM cards WHERE card_id = 'abc'").fetchone()
    assert Stats.model_validate(json.loads(row["stats"])) == stats
    assert row["style"] == "classic"


def test_steps_come_back_in_pipeline_order(db: Database, stats: Stats) -> None:
    """The ledger on the card back reads top to bottom, so order is not incidental."""
    db.create_card("abc", stats, "akamai", ("cutout", "art", "text", "compose", "render"))
    job = db.get_job("abc")
    assert [step.step for step in job.steps] == ["cutout", "art", "text", "compose", "render"]


def test_update_step_writes_what_the_service_reported(db: Database, stats: Stats) -> None:
    db.create_card("abc", stats, "akamai", ("cutout",))
    db.update_step(
        "abc",
        "cutout",
        status="done",
        model="ZhengPeng7/BiRefNet",
        license="MIT",
        ran_on="akamai-gpu0",
        ms=412,
    )
    step = db.get_job("abc").steps[0]
    assert (step.status, step.model, step.license, step.ran_on, step.ms) == (
        "done",
        "ZhengPeng7/BiRefNet",
        "MIT",
        "akamai-gpu0",
        412,
    )


def test_update_step_refuses_a_column_that_does_not_exist(db: Database, stats: Stats) -> None:
    """A typo must not be silently dropped into nothing."""
    db.create_card("abc", stats, "akamai", ("cutout",))
    with pytest.raises(ValueError, match="no such step column"):
        db.update_step("abc", "cutout", stauts="done")


def test_text_and_comps_round_trip(db: Database, stats: Stats) -> None:
    db.create_card("abc", stats, "akamai", ("text",))
    db.set_text("abc", CardText(nickname="Turnstile", tagline="Quick hands, short path"))
    db.set_comps("abc", [Comp(player="A. Vargas", season=2025, similarity=0.94)])
    job = db.get_job("abc")
    assert job.text.nickname == "Turnstile"
    assert job.comps[0].player == "A. Vargas"
    assert job.comps[0].similarity == pytest.approx(0.94)


def test_added_warnings_go_after_the_ones_already_there(db: Database, stats: Stats) -> None:
    """Photo QA writes after the cutout. The face check adds to it after compose."""
    db.create_card("abc", stats, "akamai", ())
    db.add_warnings("abc", ["cut off at the top"])
    db.add_warnings("abc", ["face small"])
    assert db.get_job("abc").warnings == ["cut off at the top", "face small"]


def test_status_and_error(db: Database, stats: Stats) -> None:
    db.create_card("abc", stats, "akamai", ())
    db.set_status("abc", "failed", "the render ran out of memory")
    job = db.get_job("abc")
    assert job.status == "failed"
    assert job.error == "the render ran out of memory"


def test_an_unknown_card_is_none(db: Database) -> None:
    assert db.get_job("nope") is None


def test_delete_removes_the_card_and_its_steps(db: Database, stats: Stats) -> None:
    db.create_card("abc", stats, "akamai", ("cutout", "art"))
    assert db.delete_card("abc") is True
    assert db.get_job("abc") is None
    with db.connect() as connection:
        left = connection.execute("SELECT COUNT(*) FROM steps WHERE card_id = 'abc'").fetchone()[0]
    assert left == 0


def test_deleting_twice_says_so(db: Database, stats: Stats) -> None:
    db.create_card("abc", stats, "akamai", ())
    assert db.delete_card("abc") is True
    assert db.delete_card("abc") is False


def test_nothing_the_pipeline_writes_after_a_delete_lands(db: Database, stats: Stats) -> None:
    """DELETE removes the rows before it stops the card's task. A step that
    finishes a moment later writes into nothing, and brings no row back."""
    db.create_card("abc", stats, "akamai", ("cutout", "render"))
    db.create_card("other", stats, "akamai", ("cutout",))
    db.delete_card("abc")

    db.set_status("abc", "done")
    db.update_step("abc", "cutout", status="done", ms=300)
    db.set_text("abc", CardText(nickname="Turnstile"))
    db.set_comps("abc", [Comp(player="A. Vargas", season=2025, similarity=0.9)])
    db.add_warnings("abc", ["no face found"])

    assert db.get_job("abc") is None
    assert db.count() == 1
    with db.connect() as connection:
        steps = connection.execute("SELECT card_id FROM steps").fetchall()
    assert [row["card_id"] for row in steps] == ["other"]


def test_older_than_finds_only_the_old_ones(db: Database, stats: Stats) -> None:
    db.create_card("new", stats, "akamai", ())
    db.create_card("old", stats, "akamai", ())
    with db.connect() as connection:
        connection.execute(
            "UPDATE cards SET created_at = ? WHERE card_id = 'old'", (time.time() - 100_000,)
        )
    assert db.older_than(time.time() - 3600) == ["old"]
    assert db.count() == 2


def test_fail_unfinished_leaves_finished_cards_alone(db: Database, stats: Stats) -> None:
    db.create_card("mid", stats, "akamai", ("cutout", "art", "text"))
    db.set_status("mid", "running")
    db.update_step("mid", "cutout", status="done", ms=300)
    db.update_step("mid", "art", status="running")
    db.create_card("waiting", stats, "akamai", ("cutout",))
    db.create_card("made", stats, "akamai", ("cutout",))
    db.update_step("made", "cutout", status="done", ms=300)
    db.set_status("made", "done")
    db.create_card("broken", stats, "akamai", ("cutout",))
    db.set_status("broken", "failed", "render failed")

    reason = "the api restarted before this card was finished"
    assert db.fail_unfinished(reason) == 2
    assert db.fail_unfinished(reason) == 0

    mid = db.get_job("mid")
    assert (mid.status, mid.error) == ("failed", reason)
    assert [(step.step, step.status, step.error) for step in mid.steps] == [
        ("cutout", "done", None),
        ("art", "failed", reason),
        ("text", "skipped", reason),
    ]
    waiting = db.get_job("waiting")
    assert (waiting.status, waiting.steps[0].status) == ("failed", "skipped")
    assert db.get_job("made").status == "done"
    assert db.get_job("made").steps[0].status == "done"
    assert db.get_job("broken").error == "render failed"
