"""Which major league hitters do these numbers resemble?

One table, a CSV from Baseball Savant: `statcast_batters.csv`, one row per
qualified batter for the 2025 season, five swing numbers each.
`comps/fetch_statcast.py` writes it, and the comment at its top says where the
rows came from.

The lookup is a nearest neighbour in z-score space. Every metric is divided by
the table's own standard deviation for that metric, so a 3 mph gap in bat speed
and a 3 degree gap in launch angle are weighed by how unusual each one is rather
than by the size of the number. Distance is Euclidean over whatever metrics the
input and the table both have, and similarity is `1 / (1 + d)`.

A comp means one thing: these numbers sit near those numbers. It says nothing
else about the player. The language model is never told who the comps are, so
it has nothing to say about a real player.

No model runs here. It is numpy on the api's CPU, which is why the comps step's
ledger row says where the api runs, and `card/render.py` leaves it out of the
rule that decides the line about GPUs on the back of the card.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from common.schemas import Comp

DATA = Path(__file__).parent / "data" / "statcast_batters.csv"

# Every other column in the CSV is a metric, keyed the same way as the metrics
# in a stats.json so the two sides join on the key with no translation table.
LABEL_COLUMNS = ("player", "season")

# A metric whose column never varies carries no information and would divide by
# zero. Floating point noise in a CSV of four decimal places is well under this.
MIN_STD = 1e-9


@dataclass(frozen=True)
class Table:
    """The rows, plus the mean and spread each column is scored against."""

    players: tuple[str, ...]
    seasons: tuple[int, ...]
    columns: dict[str, np.ndarray]
    mean: dict[str, float]
    std: dict[str, float]

    def __len__(self) -> int:
        return len(self.players)

    @property
    def metrics(self) -> tuple[str, ...]:
        """The metric keys this table can match on, in CSV column order."""
        return tuple(self.columns)


def _rows(path: Path):
    """The CSV rows, with the provenance comment at the top skipped."""
    with path.open(newline="", encoding="utf-8") as handle:
        body = (line for line in handle if not line.startswith("#"))
        yield from csv.DictReader(body)


def load_table(path: Path | str = DATA) -> Table:
    """Read the CSV. Rows with a missing or unreadable metric are dropped.

    Raises FileNotFoundError if the table is not there, and ValueError if it has
    no usable rows. The caller decides what that means: the pipeline fails the
    comps step and the card renders without a "Swings like" section.
    """
    path = Path(path)
    players: list[str] = []
    seasons: list[int] = []
    values: dict[str, list[float]] = {}
    keys: tuple[str, ...] = ()

    for row in _rows(path):
        if not keys:
            keys = tuple(k for k in row if k and k not in LABEL_COLUMNS)
            values = {key: [] for key in keys}
        try:
            numbers = [float(row[key]) for key in keys]
            season = int(row["season"])
            player = row["player"].strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not player or not all(math.isfinite(number) for number in numbers):
            continue
        players.append(player)
        seasons.append(season)
        for key, number in zip(keys, numbers, strict=True):
            values[key].append(number)

    if not players:
        raise ValueError(f"{path} has no usable rows")

    columns = {key: np.asarray(column, dtype=np.float64) for key, column in values.items()}
    return Table(
        players=tuple(players),
        seasons=tuple(seasons),
        columns=columns,
        mean={key: float(column.mean()) for key, column in columns.items()},
        # Population standard deviation. The table is the whole population here,
        # not a sample of one.
        std={key: float(column.std()) for key, column in columns.items()},
    )


def _usable(value: object) -> bool:
    """A number: an int or a float, finite, and not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def shared_metrics(metrics: dict[str, float], table: Table) -> list[str]:
    """The metrics both sides have and that the table actually varies over."""
    return [
        key
        for key in table.metrics
        if key in metrics and _usable(metrics[key]) and table.std[key] > MIN_STD
    ]


def find_comps(metrics: dict[str, float], table: Table, k: int = 3) -> list[Comp]:
    """The k closest hitters. Empty when there is nothing to compare on.

    The same input always gives the same answer: the row order in the CSV breaks
    ties, and nothing here is random.
    """
    keys = shared_metrics(metrics, table)
    if not keys or k <= 0:
        return []

    # One column per shared metric, every value in standard deviations from the
    # table's mean for that metric.
    scored = np.column_stack(
        [(table.columns[key] - table.mean[key]) / table.std[key] for key in keys]
    )
    target = np.array([(metrics[key] - table.mean[key]) / table.std[key] for key in keys])

    distance = np.linalg.norm(scored - target, axis=1)
    # Stable, so two hitters at the same distance keep the CSV's order.
    order = np.argsort(distance, kind="stable")[:k]

    return [
        Comp(
            player=table.players[i],
            season=table.seasons[i],
            similarity=1.0 / (1.0 + float(distance[i])),
            metrics={key: float(table.columns[key][i]) for key in keys},
        )
        for i in order
    ]


def lookup(metrics: dict[str, float], k: int = 3, path: Path | str = DATA) -> list[Comp]:
    """Read the table and find the closest hitters. The call the pipeline makes.

    The file is read every time. It is 225 rows and 13 KB, so that costs about
    1.5 ms, and a cache here would hand back a stale table after a re-fetch.
    """
    return find_comps(metrics, load_table(path), k)


__all__ = [
    "DATA",
    "LABEL_COLUMNS",
    "Table",
    "find_comps",
    "load_table",
    "lookup",
    "shared_metrics",
]
