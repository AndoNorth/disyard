"""One call of the largest batch through the submit route, and its resend.

It takes minutes on a T1 host, so it is marked `slow` and not `integration`: no recipe selects it,
and it runs only when asked for with `pytest packages/dispatchapi -m slow -k submit_100000`.
"""

import itertools
import json

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.pair]

JOBS = 100_000


def test_submit_100000(route, post, entries, template, db):
    head = json.dumps({"batch_key": "large", "template": template}).encode() + b"\n"
    for outcome in ("accepted", "duplicate"):
        status, answers = post(route, "acme", itertools.chain([head], entries(JOBS)))
        assert status == 200
        tally, summary = {}, None
        for answer in answers:
            if "summary" in answer:
                summary = answer["summary"]
            else:
                tally[answer["outcome"]] = tally.get(answer["outcome"], 0) + 1
        assert tally == {outcome: JOBS}
        assert summary[outcome] == JOBS
        rows = db.execute("SELECT count(*) FROM accepted_job").fetchone()[0]
        events = db.execute(
            "SELECT count(*) FROM event WHERE type = 'dev.dsy.job.accepted.v1'"
        ).fetchone()[0]
        assert (rows, events) == (JOBS, JOBS)
