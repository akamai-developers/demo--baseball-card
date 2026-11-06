"""The nearest-neighbour lookup.

The three cases the build asks for by name are at the top: the same input always
gives the same answer, one metric is enough, and no metric leaves the section off
the card. The rest are the ways a CSV can be wrong.
"""

from __future__ import annotations

import math

import pytest

from comps.nn import DATA, Table, find_comps, load_table, lookup, shared_metrics

# Five hitters, far enough apart that the nearest one is never in doubt.
SMALL = """\
# a comment the reader has to skip
# Source: nowhere
player,season,bat_speed_mph,exit_velo_mph
Slow Sam,2025,64.0,84.0
Medium Mo,2025,70.0,89.0
Quick Quinn,2025,76.0,94.0
Hard Hank,2025,72.0,97.0
Soft Sofia,2025,68.0,86.0
"""

FIXTURE_METRICS = {
    "bat_speed_mph": 68.2,
    "swing_length_ft": 7.1,
    "attack_angle_deg": 9.0,
    "exit_velo_mph": 88.5,
    "launch_angle_deg": 14.0,
}


@pytest.fixture
def small(tmp_path):
    path = tmp_path / "small.csv"
    path.write_text(SMALL)
    return load_table(path)


def write(tmp_path, text: str) -> Table:
    path = tmp_path / "table.csv"
    path.write_text(text)
    return load_table(path)


# -- the three the build names -----------------------------------------------


def test_the_same_input_always_gives_the_same_answer(small: Table) -> None:
    first = find_comps(FIXTURE_METRICS, small)
    second = find_comps(dict(reversed(list(FIXTURE_METRICS.items()))), small)
    assert first == second
    # And against the table that actually ships, not just the toy one.
    shipped = load_table()
    assert find_comps(FIXTURE_METRICS, shipped) == find_comps(FIXTURE_METRICS, shipped)


def test_one_metric_is_enough(small: Table) -> None:
    found = find_comps({"bat_speed_mph": 75.9}, small)
    assert [comp.player for comp in found] == ["Quick Quinn", "Hard Hank", "Medium Mo"]
    # Only the metric both sides had is reported as the basis for the match.
    assert set(found[0].metrics) == {"bat_speed_mph"}


def test_no_metric_means_no_comps(small: Table) -> None:
    assert find_comps({}, small) == []
    # Metrics the table has never heard of are the same thing. time_to_contact_ms
    # is a real stats.json key that Savant does not publish.
    assert find_comps({"time_to_contact_ms": 155.0}, small) == []


# -- ordering and arithmetic --------------------------------------------------


def test_a_hitter_matches_their_own_row_almost_exactly(small: Table) -> None:
    found = find_comps({"bat_speed_mph": 76.0, "exit_velo_mph": 94.0}, small)
    assert found[0].player == "Quick Quinn"
    assert found[0].similarity == pytest.approx(1.0)


def test_similarity_falls_as_distance_grows(small: Table) -> None:
    found = find_comps(FIXTURE_METRICS, small, k=5)
    assert [comp.similarity for comp in found] == sorted(
        (comp.similarity for comp in found), reverse=True
    )
    assert all(0.0 < comp.similarity <= 1.0 for comp in found)


def test_k_is_a_ceiling_not_a_promise(small: Table) -> None:
    assert len(find_comps(FIXTURE_METRICS, small, k=3)) == 3
    assert len(find_comps(FIXTURE_METRICS, small, k=99)) == len(small)
    assert find_comps(FIXTURE_METRICS, small, k=0) == []


def test_a_tie_keeps_the_order_the_csv_had(tmp_path) -> None:
    table = write(
        tmp_path,
        "player,season,bat_speed_mph\nFirst In,2025,70.0\nSecond In,2025,70.0\nOther,2025,60.0\n",
    )
    found = find_comps({"bat_speed_mph": 70.0}, table, k=2)
    assert [comp.player for comp in found] == ["First In", "Second In"]


def test_every_metric_is_weighed_by_how_unusual_it_is(tmp_path) -> None:
    """A metric with a wide spread must not drown out one with a narrow spread.

    `wide` ranges over 100 units and `narrow` over 1. Without the z-score the
    wide column would decide every match on its own.
    """
    table = write(
        tmp_path,
        "player,season,wide,narrow\nWide Match,2025,50.0,10.0\nNarrow Match,2025,150.0,10.9\n",
    )
    # 10 units off on `wide` is 0.2 standard deviations. 0.45 off on `narrow` is
    # a full one. The closer hitter in raw units is the further one in z-space.
    found = find_comps({"wide": 60.0, "narrow": 10.9}, table, k=1)
    assert found[0].player == "Narrow Match"


# -- tables that are wrong ----------------------------------------------------


def test_a_column_that_never_moves_is_ignored(tmp_path) -> None:
    table = write(
        tmp_path,
        "player,season,flat,real\nA,2025,5.0,1.0\nB,2025,5.0,9.0\n",
    )
    assert shared_metrics({"flat": 5.0, "real": 9.0}, table) == ["real"]
    found = find_comps({"flat": 999.0, "real": 9.0}, table, k=1)
    assert found[0].player == "B"


def test_a_row_with_a_hole_in_it_is_dropped(tmp_path) -> None:
    table = write(
        tmp_path,
        "player,season,bat_speed_mph\nGood,2025,70.0\nBlank,2025,\nWords,2025,fast\n,2025,71.0\n",
    )
    assert table.players == ("Good",)


def test_a_table_with_nothing_in_it_is_an_error(tmp_path) -> None:
    with pytest.raises(ValueError, match="no usable rows"):
        write(tmp_path, "player,season,bat_speed_mph\n")


def test_a_missing_table_is_an_error(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_table(tmp_path / "not-here.csv")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_a_metric_that_is_not_a_number_is_ignored(small: Table, value: float) -> None:
    assert find_comps({"bat_speed_mph": value}, small) == []
    # The finite metric alongside it still counts.
    found = find_comps({"bat_speed_mph": value, "exit_velo_mph": 94.0}, small, k=1)
    assert set(found[0].metrics) == {"exit_velo_mph"}


def test_a_boolean_is_not_a_metric(small: Table) -> None:
    """True is an int in Python, and 1 mph is not what the caller meant."""
    assert find_comps({"bat_speed_mph": True}, small) == []


# -- the table that ships -----------------------------------------------------


def test_the_shipped_table_is_real_data_not_the_synthetic_stand_in() -> None:
    """A failed re-fetch must never ship as if it were Statcast."""
    header = DATA.read_text().splitlines()[0]
    assert "SYNTHETIC" not in header.upper(), (
        f"{DATA} is the generated stand-in table. Re-run comps/fetch_statcast.py "
        "or say so in the README."
    )
    assert "Source: https://baseballsavant.mlb.com" in DATA.read_text()


def test_the_shipped_table_covers_the_metrics_the_card_asks_about() -> None:
    table = load_table()
    assert len(table) > 100
    assert set(table.metrics) == {
        "bat_speed_mph",
        "swing_length_ft",
        "attack_angle_deg",
        "exit_velo_mph",
        "launch_angle_deg",
    }
    # Real major league spreads, not placeholders.
    assert 60.0 < table.mean["bat_speed_mph"] < 80.0
    assert 80.0 < table.mean["exit_velo_mph"] < 95.0
    assert all(math.isfinite(value) and value > 0 for value in table.std.values())


def test_names_read_the_way_a_card_prints_them() -> None:
    """Savant writes "Judge, Aaron". The table has to have flipped it."""
    players = load_table().players
    assert not any("," in player for player in players)
    assert "Aaron Judge" in players


def test_lookup_reads_the_shipped_table_when_it_is_given_no_path() -> None:
    found = lookup(FIXTURE_METRICS)
    assert len(found) == 3
    assert all(comp.season == 2025 for comp in found)
    assert found == lookup(FIXTURE_METRICS, 3, DATA)


def test_the_shipped_table_says_where_its_rows_came_from() -> None:
    """The comment at the top is the table's source note: what it is, that it
    is used for nothing but comparing numbers, when it was fetched, and the
    three leaderboards it was joined from."""
    lines = DATA.read_text().splitlines()
    assert lines[0] == "# Statcast qualified batters, 2025 season."
    assert lines[1].startswith("# Public MLB data, used here only to compare")
    assert lines[2].startswith("# Written by comps/fetch_statcast.py on ")
    sources = [line for line in lines if line.startswith("# Source: ")]
    assert len(sources) == 3
    assert all("baseballsavant.mlb.com/leaderboard/" in line for line in sources)
