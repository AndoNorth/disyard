"""Negatives B of the level 4 suite: one canonical case each of the same submission twice, a
malformed and an unsupported-major intake event, and a cancel before admission.

The verdicts live in `dsy_acceptance.e2e.intake` and run against fakes in the unit tests, and this
module wires them to the cluster. N5 runs last, because it holds admission at no replica.
"""

import asyncio
import json
import time
from collections.abc import Callable

import pytest
from dsy_acceptance.clients import api, postgres, temporal
from dsy_acceptance.e2e import CONTEXT, CONTROL_API, DISPATCH, TENANT, intake, wait
from dsy_acceptance.fixtures import bundles
from dsy_acceptance.smoke import forwarded
from dsy_broker.client import connect
from dsy_broker.contract import topology
from dsy_broker.models import Outcome
from dsy_broker.suite import ACCEPTED, credentials, golden, poison
from dsy_clusterkit import CONTROL, INFRA, kubectl, state_dir
from dsy_workflow.harness import echo_params
from dsy_workflow.harness.cluster import TERMINAL

ENDS_S = 240.0


def echo_job(client: api.Api, name: str) -> dict:
    """The echo template over the workload library bundle of variant `name`."""
    return api.template(client.upload_source(bundles.variant(name)), echo_params())


@pytest.mark.scenario("N3")
def test_the_same_key_twice_is_one_job(client: api.Api, batches: Callable[[], str]):
    """Lower level proof of the variants: the dispatch API suite."""
    key = batches()
    lines = api.batch(key, echo_job(client, "n3"), 1)
    url = client.url(DISPATCH, "batches")

    def workflows(job_id: str) -> int:
        """The workflows of the job once the search index shows one, 0 after 30 s of none."""

        async def count() -> int:
            found = await temporal.executions(await temporal.connect(temporal_at), [job_id])
            return found[job_id]

        deadline = time.monotonic() + 30
        while not (found := asyncio.run(count())) and time.monotonic() < deadline:
            time.sleep(1)
        return found

    with (
        forwarded(INFRA, "pod/appdb-0", 5432) as appdb,
        forwarded(INFRA, "svc/temporal-frontend", 7233) as temporal_at,
        postgres.connect(appdb) as conn,
    ):
        try:
            intake.duplicate_submission(
                lambda: api.stream(url, lines, client.timeout_s),
                lambda: intake.rows_for_key(conn, TENANT, f"{key}/0"),
                workflows,
            )
        finally:
            client.cancel_batch(key, "e2e")


@pytest.mark.scenario("N4")
def test_malformed_and_unsupported_major_events_dead_letter(
    client: api.Api, batches: Callable[[], str]
):
    """Lower level proof of the variants: `python -m dsy_broker.suite deadletter`."""
    event = json.loads(golden()[ACCEPTED])
    queue = (
        topology()
        .intake_queue.template.replace("<tenant>", TENANT)
        .replace("<class>", event["priorityclass"])
    )
    bodies = {name: poison()[name][0] for name in intake.POISON}
    document = echo_job(client, "n4")

    def ended(job_id: str) -> str:
        def terminal() -> str | None:
            status = client.status(job_id)["status"]
            return status if status in TERMINAL else None

        return wait(terminal, ENDS_S, f"job {job_id} to end")

    with forwarded(INFRA, "svc/rabbitmq", 5672) as broker:
        connection = connect(broker, credentials(state_dir() / "rabbitmq")["dispatch"])
        try:
            intake.malformed_intake(
                intake.dead_letter_tool(broker),
                lambda body: connection.publish(queue, body) is Outcome.ACCEPTED,
                bodies,
                lambda: client.submit_one(document, batches()),
                ended,
            )
        finally:
            connection.close()


@pytest.mark.scenario("N5")
def test_a_job_cancelled_before_admission_never_starts(client: api.Api, batches: Callable[[], str]):
    """Lower level proof of the variants: the `cancel-before-start` scenario of
    `dsy_dispatchapi.smoke`."""
    document = echo_job(client, "n5")

    def cancel(job_id: str) -> dict:
        url = client.url(CONTROL_API, f"jobs/{job_id}/cancel")
        code, answer = api.request(url, "POST", json.dumps({"reason": "e2e"}).encode())
        if code != 200:
            raise AssertionError(f"the cancel of {job_id} answered {code}: {answer}")
        return answer

    def run(*args: str) -> str:
        return kubectl(CONTEXT, f"--namespace={CONTROL}", *args)

    def pods() -> list:
        listed = run("get", "pods", "-l", "app.kubernetes.io/name=admission", "-o", "json")
        return json.loads(listed)["items"]

    def exists(job_id: str) -> bool:
        async def one() -> bool:
            conn = await temporal.connect(at)
            return await intake.workflow_exists(lambda j: temporal.describe(conn, j), job_id)

        return asyncio.run(one())

    with (
        forwarded(INFRA, "svc/temporal-frontend", 7233) as at,
        intake.admission_held(run, pods),
    ):
        intake.cancel_before_admission(
            lambda: client.submit_one(document, batches()),
            cancel,
            lambda job_id: client.status(job_id)["status"],
            exists,
        )
