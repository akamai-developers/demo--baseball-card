"""Build `comps/data/statcast_batters.csv` from Baseball Savant.

    uv run python -m comps.fetch_statcast --season 2025

Three public leaderboards, joined on the MLBAM player id:

    bat-tracking                           bat speed, swing length
    bat-tracking/swing-path-attack-angle   attack angle
    statcast                               exit velocity, launch angle

Only players that appear on all three are written, so every row is complete and
`comps/nn.py` never has to reason about a missing cell. The qualified-batter
filter is Savant's own (`min=q`), so the table is a few hundred rows and the
file stays small enough to read in a diff. Savant publishes no time to contact,
so a card's time to contact is shown on its back and matched on nothing.

If Savant is unreachable or changes its columns, `--synthetic` writes a table of
made-up hitters with the same shape. Those rows are labelled "Synthetic Batter"
and the file says so at the top, in capitals, on the first line. Nothing in this
repo guesses which kind of file it has, so a synthetic table is a choice you
make and can see.

This script is run by hand when a season ends. Nothing in the pipeline calls
it, and the api never reaches Baseball Savant.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from common import __version__
from comps.nn import DATA

USER_AGENT = (
    f"baseball-card/{__version__} (https://github.com/akamai-developers/demo--baseball-card)"
)
TIMEOUT_S = 60

# The order these keys appear in is the column order of the CSV, which is also
# the order comps/nn.py reads them in. They are the same keys a stats.json uses.
METRIC_ORDER = (
    "bat_speed_mph",
    "swing_length_ft",
    "attack_angle_deg",
    "exit_velo_mph",
    "launch_angle_deg",
)


@dataclass(frozen=True)
class Leaderboard:
    """One Savant CSV endpoint and the columns wanted out of it."""

    name: str
    path: str
    id_column: str
    # Savant's column name -> the metric key used in this repo.
    metrics: dict[str, str]
    name_column: str | None = None
    query: dict[str, str] = field(default_factory=dict)

    def url(self, season: int) -> str:
        query = {"type": "batter", **self.query}
        parts = "&".join(f"{k}={v}".format(season=season) for k, v in sorted(query.items()))
        return f"https://baseballsavant.mlb.com/leaderboard/{self.path}?{parts}&csv=true"


LEADERBOARDS = (
    Leaderboard(
        name="bat tracking",
        path="bat-tracking",
        id_column="id",
        name_column="name",
        metrics={"avg_bat_speed": "bat_speed_mph", "swing_length": "swing_length_ft"},
        query={"minSwings": "q", "seasonStart": "{season}", "seasonEnd": "{season}"},
    ),
    Leaderboard(
        name="swing path",
        path="bat-tracking/swing-path-attack-angle",
        id_column="id",
        name_column="name",
        metrics={"attack_angle": "attack_angle_deg"},
        query={"minSwings": "q", "seasonStart": "{season}", "seasonEnd": "{season}"},
    ),
    Leaderboard(
        name="exit velocity",
        path="statcast",
        id_column="player_id",
        name_column="last_name, first_name",
        metrics={"avg_hit_speed": "exit_velo_mph", "avg_hit_angle": "launch_angle_deg"},
        query={"min": "q", "year": "{season}"},
    ),
)


class FetchError(RuntimeError):
    """Savant did not return a CSV this script can read."""


def flip_name(savant: str) -> str:
    """Savant writes "Judge, Aaron". A baseball card says "Aaron Judge"."""
    last, _, first = savant.partition(",")
    first, last = first.strip(), last.strip()
    return f"{first} {last}".strip() if first else last


def fetch(url: str) -> str:
    """GET the CSV. Savant answers an unknown leaderboard with an HTML page."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:  # noqa: S310
            body = reply.read().decode("utf-8-sig", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{url}: {exc}") from exc
    if body.lstrip().startswith("<"):
        raise FetchError(f"{url}: answered HTML, not CSV. The leaderboard may have moved")
    return body


def parse(body: str, board: Leaderboard) -> dict[str, dict]:
    """The wanted columns, keyed by player id. Unreadable rows are dropped."""
    reader = csv.DictReader(io.StringIO(body))
    columns = reader.fieldnames or []
    missing = [c for c in [board.id_column, *board.metrics] if c not in columns]
    if missing:
        raise FetchError(
            f"{board.name}: no column named {', '.join(missing)}. It has: {', '.join(columns)}"
        )

    out: dict[str, dict] = {}
    for row in reader:
        key = (row.get(board.id_column) or "").strip()
        if not key:
            continue
        try:
            values = {ours: float(row[theirs]) for theirs, ours in board.metrics.items()}
        except (TypeError, ValueError):
            continue
        if board.name_column:
            values["player"] = flip_name(row.get(board.name_column) or "")
        out[key] = values
    if not out:
        raise FetchError(f"{board.name}: no rows")
    return out


def join(boards: list[dict[str, dict]]) -> list[dict]:
    """Players on every leaderboard with every metric, sorted by name so the
    file diffs cleanly."""
    ids = set(boards[0])
    for board in boards[1:]:
        ids &= set(board)

    rows = []
    for key in ids:
        row: dict = {}
        for board in boards:
            row.update(board[key])
        if not row.get("player") or any(m not in row for m in METRIC_ORDER):
            continue
        rows.append(row)
    return sorted(rows, key=lambda r: (r["player"], r[METRIC_ORDER[0]]))


def synthetic_rows(count: int = 200, seed: int = 20250921) -> list[dict]:
    """A stand-in table. Plausible spreads, invented hitters, clearly labelled."""
    import numpy as np

    rng = np.random.default_rng(seed)
    # (mean, standard deviation), eyeballed from the real 2025 leaderboards.
    spread = {
        "bat_speed_mph": (71.5, 3.1),
        "swing_length_ft": (7.3, 0.5),
        "attack_angle_deg": (10.0, 3.0),
        "exit_velo_mph": (89.0, 2.6),
        "launch_angle_deg": (12.5, 4.0),
    }
    rows = []
    for i in range(count):
        row: dict = {"player": f"Synthetic Batter {i + 1:03d}"}
        for key, (mean, sd) in spread.items():
            row[key] = round(float(rng.normal(mean, sd)), 4)
        rows.append(row)
    return rows


def render_csv(rows: list[dict], season: int, sources: list[str], synthetic: bool) -> str:
    """The file, header comment and all. comps/nn.py skips lines starting with #."""
    today = dt.date.today().isoformat()
    out = io.StringIO()
    if synthetic:
        out.write("# SYNTHETIC DATA. These hitters do not exist.\n")
        out.write("# The Baseball Savant fetch failed, so this table was generated with\n")
        out.write("# comps/fetch_statcast.py --synthetic. The spreads are plausible and the\n")
        out.write("# names are not real. Re-run the fetch to replace it.\n")
    else:
        out.write(f"# Statcast qualified batters, {season} season.\n")
        out.write("# Public MLB data, used here only to compare one set of numbers to another.\n")
    out.write(f"# Written by comps/fetch_statcast.py on {today}. {len(rows)} players.\n")
    for source in sources:
        out.write(f"# Source: {source}\n")

    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["player", "season", *METRIC_ORDER])
    for row in rows:
        writer.writerow([row["player"], season, *(f"{row[key]:.4f}" for key in METRIC_ORDER)])
    return out.getvalue()


def build(season: int, synthetic: bool = False) -> tuple[str, bool]:
    """The file's text, and whether it ended up synthetic."""
    if not synthetic:
        try:
            boards = [parse(fetch(board.url(season)), board) for board in LEADERBOARDS]
        except FetchError as exc:
            print(f"the fetch failed: {exc}", file=sys.stderr)
            print("falling back to a synthetic table", file=sys.stderr)
        else:
            rows = join(boards)
            if rows:
                sources = [board.url(season) for board in LEADERBOARDS]
                return render_csv(rows, season, sources, synthetic=False), False
            print("no player appears on all three leaderboards", file=sys.stderr)

    sources = ["none. this table was generated, not fetched"]
    return render_csv(synthetic_rows(), season, sources, synthetic=True), True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", type=int, default=2025, help="season to pull. default 2025")
    parser.add_argument(
        "--out", type=Path, default=DATA, help="where to write. default the table in comps/data/"
    )
    parser.add_argument(
        "--synthetic", action="store_true", help="skip the network, write a made-up table"
    )
    args = parser.parse_args(argv)

    text, synthetic = build(args.season, synthetic=args.synthetic)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")

    rows = sum(1 for line in text.splitlines() if line and not line.startswith("#")) - 1
    kind = "SYNTHETIC" if synthetic else f"{args.season} Statcast"
    print(f"wrote {args.out} with {rows} {kind} rows")
    return 1 if synthetic and not args.synthetic else 0


if __name__ == "__main__":
    raise SystemExit(main())
