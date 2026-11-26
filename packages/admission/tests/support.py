"""What the three test directories of admission share: the manifest of the seeded jobs, the row an
accepted event becomes, the status of a row, the dev server with a client of it, and the emitter
that holds back a resume. The root conftest loads this file and the directories' conftests import it
as `admission_tests_support`."""

import pytest
from dsy_jobstate import JobRow
from dsy_workflow.activities import Emitter
from dsy_workflow.testing import client as connect_client
from dsy_workflow.testing import dev_server
from dsy_workflow.testing.control import Emit, Emitted
from temporalio import activity
from temporalio.client import Client

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def manifest(sha256: str) -> dict:
    return {
        "uri": f"s3://dsy-sources/sha256/{sha256[:2]}/{sha256}",
        "sha256": sha256,
        "size": 2048,
        "name": "manifest.json",
        "kind": "manifest",
    }


MANIFEST = manifest(SHA)


def row_of(event) -> JobRow:
    return JobRow(
        job_id=event.jobid,
        tenant=event.tenant,
        submission_key=event.data["submission_key"],
        batch_id=event.data["batch_id"],
        priority_class=event.priorityclass,
        job_class=event.jobclass,
        manifest_ref=event.data["manifest"],
        schema_version=event.dataschema,
        traceparent=event.traceparent,
        tracestate=event.tracestate,
        seed=7,
        parameters={"n": 3},
        fingerprint=SHA,
    )


@pytest.fixture
def status(db):
    return lambda job_id: db.execute(
        "SELECT status FROM accepted_job WHERE job_id = %s", (job_id,)
    ).fetchone()[0]


@pytest.fixture(scope="module")
def server(t1) -> str:
    """The address of a dev server that knows the search attributes of naming.yaml, one per test
    module, which is gone before the next module runs."""
    with dev_server(t1, search_attributes=True) as address:
        yield address


@pytest.fixture
def client(server):
    """Connects a client set up as the admission service sets it up, inside the test's loop."""

    async def connected() -> Client:
        return await connect_client(server)

    return connected


class GatedEmitter(Emitter):
    """The real `emit`, except that while `held` is set the move from `resume-pending` to
    `running` loses as if the row were still `paused`, so a workflow that applied a resume keeps
    its row at `resume-pending` and tries the move again after its backoff."""

    def __init__(self, dsn: str):
        super().__init__(dsn)
        self.held = False

    @activity.defn(name="emit")
    def emit(self, request: Emit) -> Emitted:
        move = request.transition
        if self.held and move and (move.expected, move.to) == ("resume-pending", "running"):
            return Emitted(inserted=0, lost="paused")
        return Emitter.emit(self, request)
