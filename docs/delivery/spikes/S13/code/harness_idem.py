"""Runs the workflow harness with an idempotent job start.

The harness starts 2,000 workflows at once.
When the Temporal client retries a start whose first attempt had reached the server,
the retry raises WorkflowAlreadyStartedError, the harness exits and leaves its jobs running
without pool workers.
A start that finds its workflow already there returns that workflow's handle instead.
Usage: python harness_idem.py <harness arguments>
"""

import runpy

from dsy_keys import workflow_id
from dsy_workflow import harness
from temporalio.exceptions import WorkflowAlreadyStartedError

started = harness.start


async def idempotent_start(client, tier, job, prefix=""):
    try:
        return await started(client, tier, job, prefix)
    except WorkflowAlreadyStartedError:
        return client.get_workflow_handle(workflow_id(job.job_id), result_type=str)


harness.start = idempotent_start
runpy.run_module("dsy_workflow.harness", run_name="__main__")
