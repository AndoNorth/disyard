"""The happy path scenarios against the live cluster: H1, the acceptance job, and N6, the compile
cache hit. The checks live in `dsy_acceptance.e2e.happy` and run against fakes under `tests/`.

Lower level proof of H1's variants: the workflow and aggregate suites of `just itest`.
Lower level proof of N6's variants: the cache tests of the compile and workflow packages.

Direct reads, each a row of the Direct reads table of docs/acceptance-ladder.md: the job row, its
events and the compile cache entry (Postgres, read-only), the workflow executions (Temporal), the
wasm, its build-info and the results (the object store as `workflow` and `export`), and the
aggregate pods' process lists (`kubectl exec`).
"""

import asyncio
import contextlib
import json
import time
from collections.abc import Iterator

import pytest
from dsy_acceptance.clients import api, pod, postgres, store, temporal
from dsy_acceptance.e2e import CONTEXT, happy, query
from dsy_acceptance.smoke import forwarded
from dsy_clusterkit import INFRA, WORKERS, kubectl

AGGREGATE_PODS = "aggregate-worker-"


@pytest.fixture(scope="session")
def platform(client: api.Api) -> Iterator[happy.Platform]:
    """The live surfaces behind forwards held for the session, and one event loop for Temporal."""
    loop = asyncio.new_event_loop()
    with contextlib.ExitStack() as stack:
        database = stack.enter_context(forwarded(INFRA, "pod/appdb-0", 5432))
        objects = stack.enter_context(forwarded(INFRA, "svc/objectstore", 8333))
        server = stack.enter_context(forwarded(INFRA, "svc/temporal-frontend", 7233))
        conn = stack.enter_context(postgres.connect(database))
        workflows = loop.run_until_complete(temporal.connect(server))

        def build_info_uri(wasm_uri: str) -> str | None:
            found = conn.execute(
                "SELECT build_info_uri FROM artifact WHERE wasm_uri = %s"
                " AND build_info_uri IS NOT NULL",
                (wasm_uri,),
            ).fetchone()
            return found[0] if found else None

        def aggregate_processes() -> list[set[str]]:
            listed = json.loads(
                kubectl(CONTEXT, f"--namespace={WORKERS}", "get", "pods", "-o", "json")
            )
            names = [
                p["metadata"]["name"]
                for p in listed["items"]
                if p["metadata"]["name"].startswith(AGGREGATE_PODS)
                and p["status"].get("phase") == "Running"
            ]
            return [pod.process_names(CONTEXT, WORKERS, name) for name in names]

        yield happy.Platform(
            upload=client.upload_source,
            submit=lambda document, key: happy.accepted(client, document, key),
            status=client.status,
            cancel=lambda key: client.cancel_batch(key, "e2e"),
            row=lambda job_id: postgres.job(conn, job_id),
            events=lambda job_id: postgres.events(conn, job_id),
            executions=lambda job_id: loop.run_until_complete(
                temporal.executions(workflows, [job_id])
            )[job_id],
            fetch=lambda uri, identity: store.fetch(uri, identity, f"http://{objects}"),
            build_info_uri=build_info_uri,
            aggregate_processes=aggregate_processes,
        )
    loop.close()


@pytest.mark.scenario("H1")
def test_acceptance_job(platform, acceptance_job, baseline):
    completed = baseline("completed jobs", query.COMPLETED)
    since = time.time()
    run = happy.h1(platform)
    acceptance_job["h1"] = run
    acceptance_job["query"] = query.Job(
        job_id=run.job_id,
        results=("a-sim", "b-sim", "agg"),
        since=since,
        compiled=True,
        completed=completed,
    )


@pytest.mark.scenario("N6")
def test_compile_cache_hit(platform, acceptance_job):
    if "h1" not in acceptance_job:
        pytest.skip("H1 did not produce a job to reuse")
    happy.n6(platform, acceptance_job["h1"])
