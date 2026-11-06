"""Delete cards that have outlived RETENTION_HOURS.

A photo of someone's face and a card made from it sit on a Docker volume. They
are not interesting after the demo, so a loop removes them. The default is 24
hours. DELETE /cards/{id} does the same thing at once for one card.

The loop wakes every SWEEP_EVERY_S seconds, or sooner if the retention window
is shorter than that, so a short window is still honoured.
"""

from __future__ import annotations

import asyncio
import logging
import time

from api.db import Database
from api.storage import Storage

log = logging.getLogger("sweeper")


def sweep(db: Database, storage: Storage, retention_hours: float) -> int:
    """Remove every card older than the window. Returns how many went."""
    cutoff = time.time() - retention_hours * 3600
    gone = 0
    for card_id in db.older_than(cutoff):
        storage.purge(card_id)
        db.delete_card(card_id)
        gone += 1
    if gone:
        log.info("swept %d card(s) older than %sh", gone, retention_hours)
    return gone


async def run(db: Database, storage: Storage, retention_hours: float, interval_s: float) -> None:
    """Sweep forever. Cancelled on shutdown, and an error never kills the loop."""
    period = max(10.0, min(interval_s, retention_hours * 3600))
    while True:
        try:
            await asyncio.to_thread(sweep, db, storage, retention_hours)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sweep failed. trying again in %.0fs", period)
        await asyncio.sleep(period)


__all__ = ["run", "sweep"]
