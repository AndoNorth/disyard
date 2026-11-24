"""The job-end counter is served for every job class and terminal status before any job ends."""

import re
import socket
import urllib.request

from dsy_jobstate import status_vocabulary
from dsy_obs import metrics
from dsy_workflow.service import BUCKETS, start_job_counters

CLASSES = ("short", "long")


def test_every_job_end_series_is_served_at_zero_before_any_job_ends():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    runtime = metrics.runtime(f"127.0.0.1:{port}", BUCKETS)

    start_job_counters(runtime.metric_meter, "dsy")

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
        text = response.read().decode()
    served = set(re.findall(r"^dsy_workflow_jobs_total{([^}]*)} 0$", text, re.MULTILINE))
    wanted = {
        (job_class, outcome) for job_class in CLASSES for outcome in status_vocabulary().terminal
    }
    assert wanted
    found = {
        (m["job_class"], m["outcome"])
        for labels in served
        if (m := dict(re.findall(r'(\w+)="([^"]*)"', labels)))
    }
    assert found == wanted
    for labels in served:
        assert 'namespace="dsy"' in labels
        assert 'workflow_type="JobWorkflow"' in labels
        assert 'task_queue="' in labels
