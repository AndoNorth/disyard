"""The reconciler: publishes the rows at `accepted` that hold no receipt once a grace has passed,
so a row that a submit call left unpublished, by a crash or by a broker outcome, still reaches its
intake queue.

Each row's `job.accepted` event is rebuilt from the row with its `created_at`, which is the stored
event byte for byte, and stored again before the publish, which stores nothing while the stored
copy exists and restores it once the event history dropped it. A row with a receipt is never
published again.
"""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from wdf_eventstore import insert_events
from wdf_jobstate import JobRow
from wdf_jobstate.jobs import READ_COLUMNS

from .publish import Publisher
from .submit import accepted_event

GRACE = timedelta(seconds=10)
INTERVAL_S = 5.0
ROWS_PER_S = 200

# The status term is needed because the partial index on unpublished rows has none, so it also
# holds every row cancelled or paused before its publish.
SELECT = f"""
SELECT {READ_COLUMNS} FROM accepted_job
WHERE status = 'accepted' AND published_at IS NULL AND created_at < now() - %s
  AND (created_at, accepted_job.job_id) > (%s, %s::uuid)
ORDER BY created_at, accepted_job.job_id
LIMIT %s
"""
START = (datetime.min.replace(tzinfo=UTC), "00000000-0000-0000-0000-000000000000")

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        publisher: Publisher,
        grace: timedelta = GRACE,
        interval_s: float = INTERVAL_S,
        rows_per_s: int = ROWS_PER_S,
    ):
        self.publisher, self.grace = publisher, grace
        self.interval_s, self.rows_per_s = interval_s, rows_per_s
        self._stop = threading.Event()

    def run_pass(self, conn: psycopg.Connection) -> None:
        """One pass, oldest row first and at most `rows_per_s` rows a second, on a connection of
        the dispatch login in autocommit mode. The pass ends at its first page that is not full or
        when the publisher ends a run."""
        after = START
        while not self._stop.is_set():
            started = time.monotonic()
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(SELECT, (self.grace, *after, self.rows_per_s))
                rows = [JobRow(**found) for found in cur.fetchall()]
            events = [accepted_event(row, row.created_at) for row in rows]
            insert_events(conn, events)
            if not self.publisher.publish(conn, events) or len(rows) < self.rows_per_s:
                return
            after = (rows[-1].created_at, rows[-1].job_id)
            self._stop.wait(max(0.0, 1 - (time.monotonic() - started)))

    def run(self, dsn: str) -> None:
        """Runs a pass every `interval_s` until stop() or until the publisher stops."""
        while not self._stop.is_set() and not self.publisher.stopped:
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    self.run_pass(conn)
            except psycopg.OperationalError as error:
                log.warning("the reconciler pass failed, the next one retries: %s", error)
            self._stop.wait(self.interval_s)
        if self.publisher.stopped:
            log.error("the publisher stopped, so the reconciler stops")

    def stop(self) -> None:
        self._stop.set()
