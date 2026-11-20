"""The pending patch is written only under the outstanding resume of the given sequence."""

import time
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from wft_jobstate import (
    JobNotFound,
    JobRow,
    Lost,
    get_job,
    insert_accepted,
    request_action,
    set_pending_patch,
    transition,
)

pytestmark = pytest.mark.integration
SHA = "a" * 64
PATCH = {
    "uri": f"s3://wft-sources/sha256/cc/{'c' * 64}",
    "sha256": "c" * 64,
    "size": 9,
    "name": "patch",
    "kind": "manifest",
}


@pytest.fixture
def login(appdb):
    """Connects as a fresh login user that is a member of the given group role."""
    users: list[tuple[str, psycopg.Connection]] = []
    with psycopg.connect(appdb, autocommit=True) as admin:

        def connect(group: str) -> psycopg.Connection:
            user = f"standin_{group}_{time.time_ns()}"
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'standin' IN ROLE {}").format(
                    sql.Identifier(user), sql.Identifier(group)
                )
            )
            conn = psycopg.connect(
                make_conninfo(appdb, user=user, password="standin"), autocommit=True
            )
            users.append((user, conn))
            return conn

        yield connect
        for user, conn in users:
            conn.close()
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(user)))


@pytest.fixture
def control(login):
    return login("wft_control")


@pytest.fixture
def resume_pending(appdb):
    """A job paused before it started, at `resume-pending` with its resume outstanding."""
    row = JobRow(
        job_id=str(uuid.uuid4()),
        tenant="acme",
        submission_key="key-0",
        batch_id=str(uuid.uuid4()),
        priority_class="standard",
        manifest_ref={
            "uri": f"s3://wft-sources/sha256/aa/{SHA}",
            "sha256": SHA,
            "size": 7,
            "name": "m",
            "kind": "manifest",
        },
        schema_version="wft.job.accepted/3",
        traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        seed=3,
        parameters={"a": 1},
        fingerprint="b" * 64,
    )
    with psycopg.connect(appdb, autocommit=True) as setup:
        insert_accepted(setup, [row])
        request_action(setup, row.job_id, "pause")
        transition(setup, row.job_id, "accepted", "paused", row_only=True)
        seq = request_action(setup, row.job_id, "resume").control_seq
        transition(setup, row.job_id, "paused", "resume-pending")
    return row.job_id, seq


def test_pending_patch_lands_under_the_outstanding_resume(control, resume_pending):
    job, seq = resume_pending
    assert set_pending_patch(control, job, seq, PATCH) is None
    assert get_job(control, job).pending_patch_ref == PATCH


def test_pending_patch_clears_with_none(control, resume_pending):
    job, seq = resume_pending
    set_pending_patch(control, job, seq, PATCH)
    assert set_pending_patch(control, job, seq, None) is None
    assert get_job(control, job).pending_patch_ref is None


def test_pending_patch_loses_to_a_newer_sequence(control, resume_pending):
    job, seq = resume_pending
    request_action(control, job, "cancel")
    request_action(control, job, "resume")
    assert set_pending_patch(control, job, seq, PATCH) == Lost("resume-pending")
    assert get_job(control, job).pending_patch_ref is None


def test_pending_patch_loses_to_a_cleared_action(control, resume_pending):
    job, seq = resume_pending
    transition(control, job, "resume-pending", "accepted", row_only=True)
    assert set_pending_patch(control, job, seq, PATCH) == Lost("accepted")
    assert get_job(control, job).pending_patch_ref is None


def test_pending_patch_is_refused_to_dispatch(login, resume_pending):
    job, seq = resume_pending
    dispatch = login("wft_dispatch")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        set_pending_patch(dispatch, job, seq, PATCH)


def test_pending_patch_of_an_unknown_job_raises(control):
    with pytest.raises(JobNotFound):
        set_pending_patch(control, str(uuid.uuid4()), 1, PATCH)
