"""The resume route with a patched template.

The store tests run over Postgres and SeaweedFS with Temporal faked, so the stored, indexed and
linked manifest is real. The dev server test runs JobWorkflow with the store faked, so the plan in
force, the completed tasks and the resume that admission sends are real. The store tests come first
and hold their SeaweedFS for their class alone, so the module never holds three containers.
"""

import asyncio
import json
import secrets
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest
from wdf_artifactclient.client import ArtifactClient, RetryPolicy
from wdf_artifactclient.errors import ArtifactNotFound
from wdf_compile import bundle
from wdf_dispatchapi.control import ControlRefused, control_job
from wdf_dispatchapi.gateway import TemporalGateway
from wdf_dispatchapi.models import ControlRequest
from wdf_dispatchapi.patch import Resume
from wdf_dispatchapi.resolver import StoreSourceResolver
from wdf_dispatchapi.retrier import Retrier
from wdf_dispatchapi.testing import running_status
from wdf_events import from_bytes
from wdf_events.models import ContentReference
from wdf_eventstore import events_for_job
from wdf_jobcheck import validate
from wdf_jobcheck.resolver import BundleInfo
from wdf_jobdoc.profile_files import load_profile
from wdf_jobstate import get_job, insert_accepted, transition
from wdf_plan import bind, canonical_bytes, compile_plan, manifest_reference, read_plan
from wdf_plan.canonical import MANIFEST_NAME
from wdf_storecheck.fixtures import running
from wdf_workflow.job import JobWorkflow
from wdf_workflow.models import ResumeSignal, TaskStatus
from wdf_workflow.testing import FakePool, Succeed, connect
from wdf_workflow.testing.control import LoadedPlan

pytestmark = [pytest.mark.integration, pytest.mark.pair]

NO_WAIT = RetryPolicy(sleep=lambda seconds: None)
LIMIT_S = 180
REQUESTED = "dev.wdf.job.resume.requested.v1"


@pytest.fixture(scope="session")
def profile(root):
    return load_profile("ci", root)


def template(source: dict, n: int) -> dict:
    """A compile of `source` and one checkpoint-capable simulate of `n` steps, seeded per job."""
    return {
        "schema": "urn:wdf:schema:job:1.0",
        "parameters": {"seed": "seed"},
        "tasks": {
            "build": {"type": "compile", "params": {"source": source, "entry": "job"}},
            "sim": {
                "type": "simulate",
                "inputs": [{"task": "build", "output": "component"}],
                "cpu": 0.25,
                "memory_mib": 384,
                "run_timeout_s": 600,
                "retries": 1,
                "checkpoint": True,
                "params": {"workload": "pi", "args": {"n": n}, "seed": {"param": "seed"}},
            },
        },
    }


def compiled(document: dict, profile):
    return compile_plan(
        validate(json.dumps(document).encode(), profile, Bundles()).document, profile
    )


def paused_at(*states: str):
    """The status query of a paused job whose build and simulate are at `states`."""
    tasks = [
        TaskStatus.model_validate({**running_status().tasks[0].model_dump(), "id": tid, "state": s})
        for tid, s in zip(("build", "sim"), states, strict=True)
    ]
    return running_status().model_copy(update={"job_status": "paused", "tasks": tasks})


def refused(code: str):
    return pytest.raises(ControlRefused, match=f"^{code}: ")


def resume(template: dict | None = None) -> ControlRequest:
    return ControlRequest(reason="patched", template=template)


def control_row(db, job_id: str) -> tuple:
    row = get_job(db, job_id)
    return row.status, row.requested_action, row.control_seq, row.pending_patch_ref


def history(db, job_id: str) -> list[str]:
    return [from_bytes(stored.body.encode()).type for stored in events_for_job(db, job_id)]


def links(db, job_id: str) -> list[str]:
    """The uris that the job's use rows link it to."""
    return [
        uri
        for (uri,) in db.execute(
            "SELECT a.uri FROM artifact_use u JOIN artifact a ON a.row_id = u.artifact_row_id"
            " WHERE u.job_id = %s ORDER BY a.uri",
            (job_id,),
        )
    ]


class Bundles:
    """A resolver that knows every source bundle, for a validation that reads no store."""

    def resolve_source(self, ref) -> BundleInfo:
        return BundleInfo(ref.sha256, "0" * 64, frozenset({"job"}))

    def exists(self, ref) -> bool:
        return True


class TestStore:
    @pytest.fixture(scope="class")
    @classmethod
    def shelf(cls, t1, tmp_path_factory):
        with running(t1, "seaweedfs", tmp_path_factory.mktemp("shelf")) as started:
            started.store().create_buckets()
            yield started

    @pytest.fixture
    def store(self, shelf) -> ArtifactClient:
        return ArtifactClient(shelf.endpoint, shelf.credentials["submit"], NO_WAIT)

    @pytest.fixture
    def source(self, store, tmp_path) -> dict:
        """The reference of a fresh source bundle with the module `job`, as an upload stores it."""
        tree = tmp_path / secrets.token_hex(4)
        (tree / "job").mkdir(parents=True)
        (tree / "pyproject.toml").write_text('[project]\nname = "job"\nversion = "0"\n')
        (tree / "uv.lock").write_text(f"# {secrets.token_hex(8)}\n")
        (tree / "job" / "__init__.py").write_text("def run():\n    return 0\n")
        path = bundle.write(tree, tree.parent / f"{tree.name}-out")
        return store.put_content(path, "source", bundle.NAME).model_dump(mode="json")

    @pytest.fixture
    def job(self, db, store, source, profile, gateway, kit) -> SimpleNamespace:
        """A job whose batch manifest runs 1,000 steps, paused with its build done, and the patched
        template of `n` steps."""
        plan = compiled(template(source, 1000), profile)
        store.put_content(canonical_bytes(plan), "manifest", MANIFEST_NAME)
        row = kit.new_row(secrets.token_hex(8), 7)
        row = row.model_copy(update={"manifest_ref": manifest_reference(plan)})
        insert_accepted(db, [row])
        for expected, to in (("accepted", "admitted"), ("admitted", "running")):
            transition(db, row.job_id, expected, to)
        transition(db, row.job_id, "running", "paused")
        gateway.start(row.job_id, paused_at("succeeded", "ready"))
        return SimpleNamespace(row=row, id=row.job_id, patched=lambda n: template(source, n))

    @pytest.fixture
    def call(self, store, profile, tmp_path, control_conn, logins, gateway):
        route = Resume(store, StoreSourceResolver(store, tmp_path), profile)
        with psycopg.connect(logins["artifacts"], autocommit=True) as artifacts:
            yield lambda job_id, request: route.call(
                control_conn, artifacts, gateway, "acme", job_id, request
            )

    @contextmanager
    def nothing_written(self, db, store, profile, job_id: str, document: dict):
        """Holds the row, the history and the links of the job unchanged, and the manifest of
        `document` unstored."""
        before = control_row(db, job_id), history(db, job_id), links(db, job_id)
        yield
        assert (control_row(db, job_id), history(db, job_id), links(db, job_id)) == before
        with pytest.raises(ArtifactNotFound):
            store.head(manifest_reference(compiled(document, profile)))

    def test_compatible_patch_sets_pending_ref(self, db, store, profile, job, call):
        patched = job.patched(2000)
        answer = call(job.id, resume(patched))
        assert (answer.outcome, answer.status, answer.control_seq) == (
            "requested",
            "resume-pending",
            1,
        )
        plan = compiled(patched, profile)
        ref = manifest_reference(plan)
        assert control_row(db, job.id) == ("resume-pending", "resume", 1, ref)
        assert store.get_bytes(ref, ref["size"]) == canonical_bytes(plan)
        assert ref["uri"] in links(db, job.id)
        assert history(db, job.id) == [REQUESTED]

    def test_incompatible_patch_changes_nothing(self, db, store, profile, job, gateway, call):
        gateway.workflows[job.id] = paused_at("succeeded", "succeeded")
        patched = job.patched(2000)
        with self.nothing_written(db, store, profile, job.id, patched):
            with refused("PATCH_INCOMPATIBLE") as caught:
                call(job.id, resume(patched))
        assert "completed-task-changed" in caught.value.message

    def test_invalid_patch_template(self, db, store, profile, job, call):
        broken = job.patched(2000)
        broken["tasks"]["sim"] = {"type": "simulate"}
        before = control_row(db, job.id), history(db, job.id), links(db, job.id)
        with refused("PATCH_INVALID"):
            call(job.id, resume(broken))
        assert (control_row(db, job.id), history(db, job.id), links(db, job.id)) == before

    def test_patch_not_started(self, db, store, profile, job, gateway, call, control_conn, kit):
        patched = job.patched(2000)
        # A job paused before it started has no workflow, nor has one whose workflow was purged.
        unstarted = kit.new_row(secrets.token_hex(8)).model_copy(
            update={"manifest_ref": job.row.manifest_ref}
        )
        insert_accepted(db, [unstarted])
        control_job(control_conn, gateway, "acme", unstarted.job_id, "pause", ControlRequest())
        del gateway.workflows[job.id]
        for job_id in (unstarted.job_id, job.id):
            with self.nothing_written(db, store, profile, job_id, patched):
                with refused("PATCH_NOT_STARTED"):
                    call(job_id, resume(patched))

    def test_second_patch_differs_is_pending(self, db, store, profile, job, call):
        first = call(job.id, resume(job.patched(2000)))
        second = job.patched(3000)
        with self.nothing_written(db, store, profile, job.id, second):
            with refused("PATCH_PENDING"):
                call(job.id, resume(second))
        assert get_job(db, job.id).control_seq == first.control_seq

    def test_same_patch_again_is_noop(self, db, job, call):
        first = call(job.id, resume(job.patched(2000)))
        again = call(job.id, resume(job.patched(2000)))
        assert (again.outcome, again.status, again.control_seq) == (
            "noop",
            "resume-pending",
            first.control_seq,
        )
        assert history(db, job.id) == [REQUESTED]

    def test_plain_resume_clears_the_pending_patch(self, db, job, gateway, call):
        call(job.id, resume(job.patched(2000)))
        # The workflow took the patch, ran its simulate to the end and was paused again.
        transition(db, job.id, "resume-pending", "running")
        transition(db, job.id, "running", "paused")
        gateway.workflows[job.id] = paused_at("succeeded", "succeeded")
        # The patch is the plan in force now: a template equal to the batch manifest changes the
        # completed simulate of the patch.
        with refused("PATCH_INCOMPATIBLE"):
            call(job.id, resume(job.patched(1000)))
        answer = call(job.id, resume())
        assert (answer.outcome, answer.status) == ("requested", "resume-pending")
        assert control_row(db, job.id) == ("resume-pending", "resume", 2, None)

    def test_patch_without_temporal_writes_nothing(self, db, store, profile, job, gateway, call):
        gateway.available = False
        patched = job.patched(2000)
        with self.nothing_written(db, store, profile, job.id, patched):
            with refused("TEMPORAL_UNAVAILABLE"):
                call(job.id, resume(patched))


class MemoryStore:
    """Manifests in memory by uri, and the hash of each manifest put."""

    def __init__(self, *plans):
        self.manifests = {manifest_reference(plan)["uri"]: canonical_bytes(plan) for plan in plans}
        self.puts: list[str] = []

    def get_bytes(self, reference, max_bytes: int) -> bytes:
        return self.manifests[reference["uri"]]

    def put_content(self, body: bytes, kind: str, name: str, *, conn, job_id: str):
        ref = manifest_reference(read_plan(body))
        self.manifests[ref["uri"]] = body
        self.puts.append(ref["sha256"])
        return ContentReference.model_validate(ref)


def test_patch_is_put_in_force_by_the_workflow(
    temporal, db, logins, control_conn, workflow_dsn, profile, kit
):
    """A running job is paused through the control route and the retrier, resumed with a patch of
    its simulate task through the Temporal gateway, and then sent admission's resume with the
    row's sequence and pending patch: the workflow loads the patch, checks it and runs again."""
    sha = secrets.token_hex(32)
    source = {
        "uri": f"s3://wdf-sources/sha256/{sha[:2]}/{sha}",
        "sha256": sha,
        "size": 2048,
        "name": bundle.NAME,
        "kind": "source",
    }
    base, patched = (
        compiled(template(source, 1000), profile),
        compiled(template(source, 2000), profile),
    )
    store = MemoryStore(base)
    row = kit.new_row(secrets.token_hex(8))
    row = row.model_copy(update={"manifest_ref": manifest_reference(base)})
    insert_accepted(db, [row])
    params = {"seed": row.seed, "parameters": row.parameters}

    def row_status() -> str:
        return get_job(db, row.job_id).status

    async def body() -> list[str]:
        client = await connect(temporal)
        async with (
            kit.rig(client, workflow_dsn) as control,
            FakePool(client, "compile", lambda task: Succeed()),
        ):
            for plan in (base, patched):
                control.plans[manifest_reference(plan)["uri"]] = LoadedPlan(
                    plan=bind(plan, params, profile), max_history_events=10_000
                )
            handle = await kit.start(client, db, row)
            await kit.until(lambda: row_status() == "running", "running row")
            with TemporalGateway(temporal) as gateway:
                pause = await asyncio.to_thread(
                    control_job,
                    control_conn,
                    gateway,
                    "acme",
                    row.job_id,
                    "pause",
                    ControlRequest(),
                )
                assert pause.outcome == "requested"
                await asyncio.to_thread(
                    Retrier(logins["control"], logins["reader"], gateway).deliver
                )
                await kit.until(lambda: row_status() == "paused", "paused row")
                with psycopg.connect(logins["artifacts"], autocommit=True) as artifacts:
                    answer = await asyncio.to_thread(
                        Resume(store, Bundles(), profile).call,
                        control_conn,
                        artifacts,
                        gateway,
                        "acme",
                        row.job_id,
                        resume(template(source, 2000)),
                    )
            assert (answer.outcome, answer.status) == ("requested", "resume-pending")
            pending = get_job(db, row.job_id)
            await handle.signal(
                JobWorkflow.resume,
                ResumeSignal(control_seq=pending.control_seq, manifest=pending.pending_patch_ref),
            )
            await kit.until(lambda: row_status() == "running", "running row")
            await handle.terminate()
            return control.calls

    calls = asyncio.run(asyncio.wait_for(body(), LIMIT_S))
    assert store.puts == [manifest_reference(patched)["sha256"]]
    assert get_job(db, row.job_id).pending_patch_ref == manifest_reference(patched)
    # The batch manifest at the start, then the patch on the resume.
    assert calls.count("load_plan") == 2
