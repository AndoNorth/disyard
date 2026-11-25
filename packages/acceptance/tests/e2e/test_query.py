"""H2, step 7 of the acceptance job: status, the event history, the completed counter, the trace
and the logs of the job H1 ran, with the stack on.

The lower level proof of the variants is the catalogue contract test and the live check
(docs/runbooks/drills/live-check.md). The job, and the counter baseline H1 took before it began,
come from the `acceptance_job` fixture that holds H1's job for the later scenarios.
"""

import asyncio
from contextlib import ExitStack

import pytest
from dsy_acceptance.clients import api, postgres, temporal
from dsy_acceptance.e2e import query
from dsy_acceptance.smoke import INFRA, forwarded

from .conftest import OBS_NAMESPACE


@pytest.mark.scenario("H2")
def test_query_step(acceptance_job: query.Job, client: api.Api) -> None:
    with ExitStack() as stack:

        def address(namespace: str, resource: str, port: int) -> str:
            return stack.enter_context(forwarded(namespace, resource, port))

        appdb = address(INFRA, "pod/appdb-0", 5432)
        frontend = address(INFRA, "svc/temporal-frontend", 7233)
        tempo = f"http://{address(OBS_NAMESPACE, 'svc/tempo', 3200)}"
        loki = f"http://{address(OBS_NAMESPACE, 'svc/loki', 3100)}"
        conn = stack.enter_context(postgres.connect(appdb))
        job_id = acceptance_job.job_id

        async def fetch_history() -> list:
            return await temporal.history(await temporal.connect(frontend), job_id)

        query.run(
            acceptance_job,
            status=lambda: client.status(job_id),
            rows=lambda: postgres.events(conn, job_id),
            history=lambda: asyncio.run(fetch_history()),
            tempo=tempo,
            loki=loki,
        )
