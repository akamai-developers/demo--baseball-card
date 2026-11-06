"""Building the Statcast table.

Nothing here touches the network. `fetch` is replaced in every test that needs
it, so the suite runs the same whether or not Baseball Savant is up.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import types

import pytest

from comps.fetch_statcast import (
    LEADERBOARDS,
    METRIC_ORDER,
    USER_AGENT,
    FetchError,
    build,
    fetch,
    flip_name,
    join,
    main,
    parse,
    render_csv,
    synthetic_rows,
)
from comps.nn import DATA, load_table

BAT_TRACKING = (
    '"id","name","avg_bat_speed","swing_length"\n'
    '691406,"Caminero, Junior","79.5587",8.7202\n'
    '592450,"Judge, Aaron","76.9405",8.1027\n'
    '111111,"Nobody, Elsewhere","70.0",7.0\n'
)
SWING_PATH = (
    '"id","name","attack_angle"\n691406,"Caminero, Junior",8.6\n592450,"Judge, Aaron",14.0292\n'
)
EXIT_VELO = (
    '"last_name, first_name","player_id","avg_hit_speed","avg_hit_angle"\n'
    '"Caminero, Junior",691406,93.1,12.2\n'
    '"Judge, Aaron",592450,95.4,19.0\n'
)


def boards(bt=BAT_TRACKING, sp=SWING_PATH, ev=EXIT_VELO) -> list[dict]:
    return [parse(body, board) for body, board in zip((bt, sp, ev), LEADERBOARDS, strict=True)]


# -- names --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("savant", "card"),
    [
        ("Judge, Aaron", "Aaron Judge"),
        ("García, Adolis", "Adolis García"),
        ("Tatis Jr., Fernando", "Fernando Tatis Jr."),
        ("Ohtani, Shohei", "Shohei Ohtani"),
        # A name with no comma is already the right way round.
        ("Ichiro", "Ichiro"),
        ("", ""),
    ],
)
def test_names_are_flipped_for_the_card(savant: str, card: str) -> None:
    assert flip_name(savant) == card


# -- reading a leaderboard ----------------------------------------------------


def test_the_wanted_columns_are_pulled_out_and_renamed() -> None:
    rows = parse(BAT_TRACKING, LEADERBOARDS[0])
    assert rows["592450"] == {
        "bat_speed_mph": 76.9405,
        "swing_length_ft": 8.1027,
        "player": "Aaron Judge",
    }


def test_a_leaderboard_that_lost_a_column_is_an_error() -> None:
    with pytest.raises(FetchError, match="avg_bat_speed"):
        parse('"id","name"\n1,"Judge, Aaron"\n', LEADERBOARDS[0])


def test_a_leaderboard_with_no_rows_is_an_error() -> None:
    with pytest.raises(FetchError, match="no rows"):
        parse('"id","name","avg_bat_speed","swing_length"\n', LEADERBOARDS[0])


def test_a_row_that_will_not_parse_is_dropped_not_fatal() -> None:
    body = BAT_TRACKING + '999999,"Broken, Row","n/a",7.0\n'
    assert "999999" not in parse(body, LEADERBOARDS[0])


def test_savant_answering_html_is_an_error(monkeypatch) -> None:
    """An unknown leaderboard returns an error page with HTTP 200 on it."""

    class FakeReply:
        def read(self):
            return b"<!DOCTYPE html>\n<html lang='en_US'>"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeReply())
    with pytest.raises(FetchError, match="answered HTML"):
        fetch("https://baseballsavant.mlb.com/leaderboard/gone?csv=true")


def test_the_season_goes_into_every_url() -> None:
    for board in LEADERBOARDS:
        url = board.url(2024)
        assert "2024" in url and "csv=true" in url and "type=batter" in url
        assert "{season}" not in url


# -- joining ------------------------------------------------------------------


def test_only_players_on_every_leaderboard_are_kept() -> None:
    rows = join(boards())
    # 111111 is on bat tracking alone, so it is not in the table.
    assert [row["player"] for row in rows] == ["Aaron Judge", "Junior Caminero"]
    assert all(metric in rows[0] for metric in METRIC_ORDER)


def test_a_row_missing_a_metric_after_the_join_is_dropped() -> None:
    thin = '"id","name","attack_angle"\n592450,"Judge, Aaron",14.0\n691406,"Caminero, Junior",\n'
    rows = join(boards(sp=thin))
    assert [row["player"] for row in rows] == ["Aaron Judge"]


def test_nothing_in_common_is_an_empty_table() -> None:
    other = '"id","name","attack_angle"\n7,"Someone, Else",9.0\n'
    assert join(boards(sp=other)) == []


# -- writing ------------------------------------------------------------------


def test_the_file_says_where_it_came_from_and_when(tmp_path) -> None:
    text = render_csv(join(boards()), 2025, ["https://example.test/board"], synthetic=False)
    assert "# Statcast qualified batters, 2025 season." in text
    assert "# Source: https://example.test/board" in text
    assert "comps/fetch_statcast.py on 20" in text

    path = tmp_path / "t.csv"
    path.write_text(text)
    table = load_table(path)
    assert table.players == ("Aaron Judge", "Junior Caminero")
    assert table.seasons == (2025, 2025)
    assert table.metrics == METRIC_ORDER


def test_a_generated_table_says_so_on_the_first_line() -> None:
    text = render_csv(synthetic_rows(4), 2025, ["none"], synthetic=True)
    assert text.splitlines()[0] == "# SYNTHETIC DATA. These hitters do not exist."
    assert "Synthetic Batter 001" in text


def test_the_generated_table_is_the_same_every_time() -> None:
    assert synthetic_rows(10) == synthetic_rows(10)
    rows = synthetic_rows(200)
    assert len(rows) == 200
    assert all(set(row) == {"player", *METRIC_ORDER} for row in rows)


def test_the_generated_table_loads_and_looks_up(tmp_path) -> None:
    path = tmp_path / "s.csv"
    path.write_text(render_csv(synthetic_rows(50), 2025, ["none"], synthetic=True))
    table = load_table(path)
    assert len(table) == 50
    assert 65.0 < table.mean["bat_speed_mph"] < 78.0


# -- the fallback -------------------------------------------------------------


def test_a_failed_fetch_falls_back_to_a_generated_table(monkeypatch, capsys) -> None:
    def boom(url: str) -> str:
        raise FetchError(f"{url}: connection refused")

    monkeypatch.setattr("comps.fetch_statcast.fetch", boom)
    text, synthetic = build(2025)

    assert synthetic
    assert "SYNTHETIC" in text.splitlines()[0]
    assert "the fetch failed" in capsys.readouterr().err


def test_a_join_with_nothing_in_it_falls_back_too(monkeypatch, capsys) -> None:
    bodies = iter([BAT_TRACKING, '"id","name","attack_angle"\n7,"No, One",9.0\n', EXIT_VELO])
    monkeypatch.setattr("comps.fetch_statcast.fetch", lambda url: next(bodies))
    text, synthetic = build(2025)

    assert synthetic
    assert "no player appears on all three" in capsys.readouterr().err
    assert "SYNTHETIC" in text


def test_a_good_fetch_writes_real_rows(monkeypatch, tmp_path, capsys) -> None:
    bodies = iter([BAT_TRACKING, SWING_PATH, EXIT_VELO])
    monkeypatch.setattr("comps.fetch_statcast.fetch", lambda url: next(bodies))
    out = tmp_path / "written.csv"

    assert main(["--season", "2025", "--out", str(out)]) == 0
    assert "2 2025 Statcast rows" in capsys.readouterr().out
    assert load_table(out).players == ("Aaron Judge", "Junior Caminero")


def test_a_silent_fallback_is_reported_in_the_exit_code(monkeypatch, tmp_path) -> None:
    """A cron that re-fetches has to be able to tell that it got made-up data."""
    monkeypatch.setattr("comps.fetch_statcast.fetch", lambda url: "<!DOCTYPE html>")
    out = tmp_path / "written.csv"

    assert main(["--out", str(out)]) == 1
    # Asking for the stand-in on purpose is not a failure.
    assert main(["--synthetic", "--out", str(out)]) == 0


def test_the_script_writes_the_shipped_table_unless_told_otherwise(monkeypatch, tmp_path) -> None:
    """Without --out it writes comps.nn.DATA, the table the pipeline reads."""
    bodies = iter([BAT_TRACKING, SWING_PATH, EXIT_VELO])
    monkeypatch.setattr("comps.fetch_statcast.fetch", lambda url: next(bodies))
    shipped = tmp_path / "statcast_batters.csv"
    monkeypatch.setattr("comps.fetch_statcast.DATA", shipped)

    assert main(["--season", "2025"]) == 0
    assert load_table(shipped).players == ("Aaron Judge", "Junior Caminero")


def test_it_names_this_repo_to_baseball_savant() -> None:
    assert USER_AGENT.startswith("baseball-card/")
    assert "github.com/akamai-developers/demo--baseball-card" in USER_AGENT


# -- the batters' table, as it was --------------------------------------------


def savant_bodies_for(path) -> list[str]:
    """The three leaderboards that would have made the committed batters'
    table: one row per player, the card's name in Savant's name column (a name
    with no comma comes through flip_name as it is), an id of our own."""
    rows = list(csv.DictReader(line for line in path.read_text().splitlines() if line[:1] != "#"))
    bodies = []
    for board in LEADERBOARDS:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow([board.id_column, board.name_column, *board.metrics])
        for number, row in enumerate(rows, 1):
            writer.writerow(
                [number, row["player"], *(row[ours] for ours in board.metrics.values())]
            )
        bodies.append(out.getvalue())
    return bodies


def test_the_batters_table_is_written_byte_for_byte_as_it_was(monkeypatch, tmp_path) -> None:
    """The script writes the committed file, header and all, from the
    leaderboards that made it, on the day it was written."""
    shipped = DATA.read_text(encoding="utf-8")
    bodies = iter(savant_bodies_for(DATA))
    monkeypatch.setattr("comps.fetch_statcast.fetch", lambda url: next(bodies))

    class Then(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 21)

    monkeypatch.setattr("comps.fetch_statcast.dt", types.SimpleNamespace(date=Then))
    out = tmp_path / "batters.csv"
    assert main(["--season", "2025", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == shipped
