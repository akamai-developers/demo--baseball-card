"""The stats a card is made from, and the card id."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.schemas import METRICS, STYLES, SWING_METRICS, CardJob, Stats, new_card_id


def test_name_is_the_only_required_field() -> None:
    stats = Stats(name="Sam Rivera")
    assert stats.name == "Sam Rivera"
    assert stats.metrics == {}
    assert stats.number is None


def test_a_full_stats_file_parses() -> None:
    stats = Stats(
        name="Sam Rivera",
        number=23,
        position="SS",
        bats="R",
        throws="R",
        height_in=72,
        weight_lb=190,
        hometown="Chicago, IL",
        metrics={"bat_speed_mph": 68.2, "exit_velo_mph": 88.5},
    )
    assert stats.metrics["bat_speed_mph"] == 68.2


def test_missing_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Stats()


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_name_is_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        Stats(name=name)


def test_a_name_of_sixty_characters_fits_and_sixty_one_does_not() -> None:
    assert len(Stats(name="A" * 60).name) == 60
    with pytest.raises(ValidationError):
        Stats(name="A" * 61)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Stats(name="Sam", nickname="Slugger")


def test_style_is_not_a_stat() -> None:
    """The style is its own form field on POST /cards, not part of the stats."""
    with pytest.raises(ValidationError):
        Stats(name="Sam", style="akamai")


def test_two_styles() -> None:
    assert STYLES == ("akamai", "classic")


def test_the_card_knows_six_swing_numbers() -> None:
    assert tuple(METRICS) == SWING_METRICS
    assert SWING_METRICS == (
        "bat_speed_mph",
        "exit_velo_mph",
        "launch_angle_deg",
        "swing_length_ft",
        "attack_angle_deg",
        "time_to_contact_ms",
    )
    for metric in METRICS.values():
        assert metric.low < metric.high


@pytest.mark.parametrize("key", SWING_METRICS)
def test_both_ends_of_each_range_are_in_it(key: str) -> None:
    metric = METRICS[key]
    assert Stats(name="Sam", metrics={key: metric.low}).metrics[key] == metric.low
    assert Stats(name="Sam", metrics={key: metric.high}).metrics[key] == metric.high


@pytest.mark.parametrize(
    ("metrics", "said"),
    [
        ({"bat_speed_mph": 120}, "Bat Speed is 120 mph. It has to be from 50 to 85 mph"),
        ({"launch_angle_deg": -40}, "Launch Angle is -40 deg. It has to be from -10 to 35 deg"),
        ({"time_to_contact_ms": 99}, "Time to Contact is 99 ms. It has to be from 100 to 220 ms"),
        ({"pitch_speed_mph": 72.0}, "pitch_speed_mph is not a number this card shows"),
        ({"bat_speed_mph": float("nan")}, "Bat Speed is not a number"),
        ({"exit_velo_mph": float("inf")}, "Exit Velocity is not a number"),
    ],
)
def test_a_number_the_card_cannot_show_is_refused_in_a_sentence(metrics: dict, said: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Stats(name="Sam", metrics=metrics)
    assert said in str(caught.value)


def test_card_id_is_128_bits_and_url_safe() -> None:
    ids = {new_card_id() for _ in range(200)}
    assert len(ids) == 200
    for card_id in ids:
        assert len(card_id) == 22
        assert all(c.isalnum() or c in "-_" for c in card_id)


def test_card_job_defaults() -> None:
    job = CardJob(card_id=new_card_id(), status="queued")
    assert job.steps == []
    assert job.comps == []
    assert job.error is None
    assert job.text.nickname == ""
