"""The observability wiring of the admission service: the JSON claim line with the job and its
trace, and the cap in force served by scope."""

import asyncio
import io
import json
import logging
import re
import urllib.request
from datetime import UTC, datetime
from types import SimpleNamespace

from wft_admission.policy import Caps
from wft_admission.service import _timed
from wft_admission.service.metrics import Metrics
from wft_broker.tenancy import load
from wft_obs import logs

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE_ID}-b7ad6b7169203331-01"


def test_claim_line_carries_job_and_trace(runtime):
    caps = Caps.of(load("ci"))
    metrics = Metrics(runtime.metric_meter, ["standard"], caps)
    row = SimpleNamespace(
        job_id="job-1",
        traceparent=TRACEPARENT,
        priority_class="standard",
        created_at=datetime.now(UTC),
    )

    async def start(row, accepted):
        return "started"

    stream = io.StringIO()
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    logs.install("admission", stream)
    try:
        assert asyncio.run(_timed(start, metrics)(row, None)) == "started"
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:], root.level = saved
    [line] = [json.loads(text) for text in stream.getvalue().splitlines()]
    assert (line["component"], line["job_id"], line["trace_id"]) == ("admission", "job-1", TRACE_ID)
    assert set(line) <= {*logs.CORE, *logs.FIELDS}


def test_cap_gauge_by_scope(runtime, metrics_port):
    caps = Caps.of(load("ci"))
    Metrics(runtime.metric_meter, ["standard"], caps)
    body = urllib.request.urlopen(f"http://127.0.0.1:{metrics_port}/metrics").read().decode()
    for scope, cap in (("global", caps.global_), ("open", caps.open)):
        assert re.search(
            rf'wft_admission_cap_jobs\{{[^}}]*scope="{scope}"[^}}]*\}} {cap}\b', body
        ), body
