"""Jobs and step timings, in one SQLite file on the data volume.

Two tables. `cards` holds the job: what it was made from, the stats and the
style, and everything the language model and the nearest-neighbour lookup
produced. `steps` holds the ledger that ends up on the back of the card, one
row per pipeline step. `sqlite3 cards.db` on the data volume is the way to look
at a card that came out wrong, with its intermediates in work/.

Every call opens its own connection. SQLite in WAL mode lets readers run while a
writer holds the file, and a five second busy timeout covers the moment two
pipeline branches finish together. There is no connection pool and no ORM,
because the whole database is a few kilobytes per card.

DELETE /cards/{id} removes a card's rows before it stops the card's pipeline.
Every write the pipeline makes names its card, so once the rows are gone a step
that finishes a moment later writes nothing.

These functions block. Call them from async code with asyncio.to_thread.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path

from common.schemas import CardJob, CardText, Comp, Stats, Step

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id    TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    stats      TEXT NOT NULL,
    style      TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '{}',
    comps      TEXT NOT NULL DEFAULT '[]',
    warnings   TEXT NOT NULL DEFAULT '[]',
    error      TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    card_id  TEXT NOT NULL,
    step     TEXT NOT NULL,
    position INTEGER NOT NULL,
    status   TEXT NOT NULL,
    model    TEXT,
    license  TEXT,
    ran_on   TEXT,
    ms       INTEGER,
    error    TEXT,
    source   TEXT,
    PRIMARY KEY (card_id, step)
);

CREATE INDEX IF NOT EXISTS cards_created_at ON cards (created_at);
"""

# The step columns a pipeline may set.
STEP_COLUMNS = frozenset({"status", "model", "license", "ran_on", "ms", "error", "source"})


class Database:
    """The card database. One instance per process, holding only a path."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def init(self) -> None:
        """Create the file and the tables. Safe to call on every start."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    # -- writes --------------------------------------------------------------

    def create_card(self, card_id: str, stats: Stats, style: str, steps: Sequence[str]) -> None:
        """Insert a queued job and one queued row per step, in pipeline order."""
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO cards (card_id, status, created_at, updated_at, stats, style)"
                " VALUES (?, 'queued', ?, ?, ?, ?)",
                (card_id, now, now, stats.model_dump_json(), style),
            )
            connection.executemany(
                "INSERT INTO steps (card_id, step, position, status) VALUES (?, ?, ?, 'queued')",
                [(card_id, name, index) for index, name in enumerate(steps)],
            )

    def set_status(self, card_id: str, status: str, error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE cards SET status = ?, error = ?, updated_at = ? WHERE card_id = ?",
                (status, error, time.time(), card_id),
            )

    def set_text(self, card_id: str, text: CardText) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE cards SET text = ?, updated_at = ? WHERE card_id = ?",
                (text.model_dump_json(), time.time(), card_id),
            )

    def set_comps(self, card_id: str, comps: Sequence[Comp]) -> None:
        payload = json.dumps([comp.model_dump() for comp in comps])
        with self.connect() as connection:
            connection.execute(
                "UPDATE cards SET comps = ?, updated_at = ? WHERE card_id = ?",
                (payload, time.time(), card_id),
            )

    def add_warnings(self, card_id: str, warnings: Sequence[str]) -> None:
        """Append to the warnings, after the ones photo QA wrote.

        A read and a write, not one statement. Only the job's own pipeline writes
        its warnings, photo QA first and the face check after compose, so nothing
        else can land in between.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT warnings FROM cards WHERE card_id = ?", (card_id,)
            ).fetchone()
            if row is None:
                return
            merged = [*json.loads(row["warnings"] or "[]"), *warnings]
            connection.execute(
                "UPDATE cards SET warnings = ?, updated_at = ? WHERE card_id = ?",
                (json.dumps(merged), time.time(), card_id),
            )

    def update_step(self, card_id: str, step: str, **fields: object) -> None:
        """Set some columns on one step row. Unknown columns are refused, not ignored."""
        unknown = set(fields) - STEP_COLUMNS
        if unknown:
            raise ValueError(f"no such step column: {sorted(unknown)}")
        if not fields:
            return
        # The names come from STEP_COLUMNS, never from a caller.
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE steps SET {assignments} WHERE card_id = ? AND step = ?",  # noqa: S608
                (*fields.values(), card_id, step),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE cards SET updated_at = ? WHERE card_id = ?", (time.time(), card_id)
                )

    def fail_unfinished(self, reason: str) -> int:
        """Mark every card still queued or running as failed. Returns how many.

        Called once, when the api starts. The api is one process and holds each
        card's pipeline in memory, so when it starts nothing can be in flight: a
        card still queued or running was cut off when the last process stopped.
        Its running steps fail and its queued ones are skipped, with the reason.
        """
        with self.connect() as connection:
            # The steps first, while their cards still say queued or running.
            for old, new in (("running", "failed"), ("queued", "skipped")):
                connection.execute(
                    "UPDATE steps SET status = ?, error = ? WHERE status = ? AND card_id IN"
                    " (SELECT card_id FROM cards WHERE status IN ('queued', 'running'))",
                    (new, reason, old),
                )
            cursor = connection.execute(
                "UPDATE cards SET status = 'failed', error = ?, updated_at = ?"
                " WHERE status IN ('queued', 'running')",
                (reason, time.time()),
            )
            return cursor.rowcount

    def delete_card(self, card_id: str) -> bool:
        """Remove the job and its steps. False when there was nothing to remove."""
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM cards WHERE card_id = ?", (card_id,))
            connection.execute("DELETE FROM steps WHERE card_id = ?", (card_id,))
            return cursor.rowcount > 0

    # -- reads ---------------------------------------------------------------

    def get_job(self, card_id: str) -> CardJob | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            if row is None:
                return None
            steps = connection.execute(
                "SELECT * FROM steps WHERE card_id = ? ORDER BY position", (card_id,)
            ).fetchall()
        return CardJob(
            card_id=row["card_id"],
            status=row["status"],
            steps=[
                Step(
                    step=step["step"],
                    status=step["status"],
                    model=step["model"],
                    license=step["license"],
                    ran_on=step["ran_on"],
                    ms=step["ms"],
                    error=step["error"],
                    source=step["source"],
                )
                for step in steps
            ],
            text=CardText.model_validate_json(row["text"]),
            comps=[Comp.model_validate(comp) for comp in json.loads(row["comps"])],
            warnings=list(json.loads(row["warnings"] or "[]")),
            error=row["error"],
        )

    def older_than(self, cutoff: float) -> list[str]:
        """Card ids created before `cutoff`, oldest first. The sweeper's input."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT card_id FROM cards WHERE created_at < ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        return [row["card_id"] for row in rows]

    def count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"])


__all__ = ["SCHEMA", "STEP_COLUMNS", "Database"]
