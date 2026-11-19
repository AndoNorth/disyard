"""The submit route against Postgres and SeaweedFS, on the dispatch and artifacts logins."""

import io
import json

import pytest
from wdf_artifactindex import Deleted, Purged, purge, tombstone
from wdf_dispatchapi.objects import Refused
from wdf_events.models import ContentReference
from wdf_jobstate import transition
from wdf_keys import batch_id, job_id
from wdf_storecheck.provision import ADMIN
from wdf_storecheck.s3 import client as s3_client

pytestmark = [pytest.mark.integration, pytest.mark.pair]

ACCEPTED = "dev.wdf.job.accepted.v1"


def line(data: dict) -> bytes:
    return json.dumps(data).encode() + b"\n"


def header(template: dict, batch_key: str = "b", **fields) -> bytes:
    return line({"batch_key": batch_key, "template": template, **fields})


def entry(index: int, seed: int | None = None, **fields) -> bytes:
    seed = index if seed is None else seed
    return line({"index": index, "seed": seed, "parameters": {"n": index}, **fields})


def counts(db) -> tuple[int, int]:
    """The stored rows and `job.accepted` events."""
    rows = db.execute("SELECT count(*) FROM accepted_job").fetchone()[0]
    events = db.execute("SELECT count(*) FROM event WHERE type = %s", (ACCEPTED,)).fetchone()[0]
    return rows, events


def outcomes(answers: list[dict]) -> list[tuple]:
    return [(a.get("index"), a["outcome"], a.get("reason")) for a in answers if "outcome" in a]


class Unread(io.RawIOBase):
    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        raise AssertionError("the body was read")


class Paused(io.RawIOBase):
    """A body that hands over its parts one read at a time and runs `between` before the second,
    as a client that sends the next entry only after it read the answer to the first."""

    def __init__(self, parts: list[bytes], between):
        self.parts, self.between = parts, between

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self.parts:
            return 0
        if len(self.parts) == 1:
            self.between()
        part = self.parts.pop(0)
        buffer[: len(part)] = part
        return len(part)


def test_entry_outcomes_matrix(make_submit, call, template, db):
    submit = make_submit()
    first = call(submit, [header(template), entry(0), entry(1)])
    answers = call(
        submit,
        [
            header(template),
            entry(0),
            entry(1, seed=99),
            entry(2),
            entry(2),
            entry(3, priority_class="gold"),
            entry(4, job_class="huge"),
            line({"index": 5, "seed": 5, "parameters": {"n": "five"}}),
            b'{"index": 6, "seed": -1, "parameters": {}}\n',
        ],
    )
    assert outcomes(answers) == [
        (0, "duplicate", None),
        (1, "rejected", "KEY_CONFLICT"),
        (2, "accepted", None),
        (2, "rejected", "DUPLICATE_INDEX"),
        (3, "rejected", "UNKNOWN_PRIORITY_CLASS"),
        (4, "rejected", "UNKNOWN_JOB_CLASS"),
        (5, "rejected", "INVALID_PARAMS"),
        (6, "rejected", "MALFORMED_LINE"),
    ]
    assert answers[0]["status"] == "accepted"
    assert answers[0]["job_id"] == first[0]["job_id"]
    assert answers[0]["trace_id"] == first[0]["trace_id"]
    assert answers[1]["job_id"] == first[1]["job_id"]
    assert answers[6]["issues"] == [{"path": "/parameters/n", "code": "parameter-type-mismatch"}]
    assert answers[-1] == {
        "summary": {"batch_id": batch_id("acme", "b"), "accepted": 1, "duplicate": 1, "rejected": 6}
    }
    assert counts(db) == (3, 3)
    seed = db.execute("SELECT seed FROM accepted_job WHERE submission_key = 'b/1'").fetchone()
    assert seed == (1,)


def test_links_exist_before_rows(make_submit, call, template, db):
    # Every row insert asserts that its job already holds a committed link to the manifest and to
    # the source bundle, as the reaper would see them.
    db.execute(
        "CREATE FUNCTION links_first() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$"
        " BEGIN IF (SELECT count(*) FROM artifact_use WHERE job_id = NEW.job_id) < 2 THEN"
        " RAISE EXCEPTION 'job % has no links', NEW.job_id; END IF; RETURN NEW; END $$"
    )
    db.execute(
        "CREATE TRIGGER links_first BEFORE INSERT ON accepted_job"
        " FOR EACH ROW EXECUTE FUNCTION links_first()"
    )
    answers = call(make_submit(chunk_entries=2), [header(template), *map(entry, range(5))])
    assert [a["outcome"] for a in answers[:-1]] == ["accepted"] * 5
    links = db.execute(
        "SELECT a.kind, count(*) FROM artifact_use u"
        " JOIN artifact a ON a.row_id = u.artifact_row_id GROUP BY a.kind ORDER BY a.kind"
    ).fetchall()
    assert links == [("manifest", 5), ("source", 5)]


def test_job_class_written_once(make_submit, call, template, db):
    answers = call(make_submit(), [header(template), entry(0), entry(1, job_class="long")])
    assert [a["outcome"] for a in answers[:-1]] == ["accepted", "accepted"]
    stored = db.execute(
        "SELECT j.submission_key, j.job_class, e.body FROM accepted_job j"
        " JOIN event e ON e.job_id = j.job_id AND e.type = %s ORDER BY j.submission_key",
        (ACCEPTED,),
    ).fetchall()
    assert [(key, cls, json.loads(body)["jobclass"]) for key, cls, body in stored] == [
        ("b/0", "short", "short"),
        ("b/1", "long", "long"),
    ]


def test_unknown_tenant_reads_no_body(make_submit, call):
    with pytest.raises(Refused) as refused:
        call(make_submit(), io.BufferedReader(Unread()), tenant="initech")
    assert (refused.value.code, refused.value.http) == ("UNKNOWN_TENANT", 404)


def test_one_template_manifest_per_batch(make_submit, call, template, store, db):
    submit = make_submit(chunk_entries=2)
    call(submit, [header(template), *map(entry, range(5))])
    call(submit, [header(template), *map(entry, range(5))])
    manifests = db.execute(
        "SELECT uri, sha256, size FROM artifact WHERE kind = 'manifest'"
    ).fetchall()
    assert len(manifests) == 1
    uri, sha256, size = manifests[0]
    store.head(ContentReference(uri=uri, sha256=sha256, size=size, name="m", kind="manifest"))
    refs = db.execute("SELECT DISTINCT manifest_ref ->> 'uri' FROM accepted_job").fetchall()
    assert refs == [(uri,)]
    uses = db.execute(
        "SELECT count(*) FROM artifact_use u JOIN artifact a ON a.row_id = u.artifact_row_id"
        " WHERE a.uri = %s",
        (uri,),
    ).fetchone()
    assert uses == (5,)
    # Each job's seed and parameters live on its row, and never in its event.
    rows = db.execute("SELECT seed, parameters FROM accepted_job ORDER BY seed").fetchall()
    assert rows == [(index, {"n": index}) for index in range(5)]
    for (body,) in db.execute("SELECT body FROM event WHERE type = %s", (ACCEPTED,)):
        assert set(json.loads(body)["data"]) == {"manifest", "batch_id", "submission_key"}


def test_purged_object_stops_the_call(make_submit, call, template, source, shelf, db):
    submit = make_submit(chunk_entries=1)

    def reap():
        # The only job that holds the bundle ends, and the reaper tombstones and purges it.
        transition(db, job_id("acme", "b/0"), "accepted", "cancelled", row_only=True)
        uri = source["uri"]
        (now,) = db.execute("SELECT now()").fetchone()
        assert isinstance(tombstone(db, uri, now, grace=now - now), Deleted)
        with db.transaction():
            (now,) = db.execute("SELECT now()").fetchone()
            assert isinstance(purge(db, uri, now, now - now, now - now), Purged)
            s3 = s3_client(shelf.endpoint, shelf.credentials[ADMIN])
            s3.delete_object(Bucket="wdf-sources", Key=uri.split("/", 3)[3])

    body = Paused([header(template) + entry(0), entry(1)], reap)
    answers = call(submit, io.BufferedReader(body, buffer_size=1 << 20))
    assert outcomes(answers) == [(0, "accepted", None)]
    assert answers[-1]["error"]["code"] == "OBJECT_MISSING"
    assert counts(db) == (1, 1)


def test_tenant_scoping(make_submit, call, template, tenancy, db):
    tenants = {**tenancy.tenants, "globex": tenancy.tenants["acme"]}
    submit = make_submit(tenancy=tenancy.model_copy(update={"tenants": tenants}))
    acme = call(submit, [header(template), entry(0)], tenant="acme")
    globex = call(submit, [header(template), entry(0)], tenant="globex")
    assert acme[0]["outcome"] == globex[0]["outcome"] == "accepted"
    assert (
        acme[0]["job_id"] == job_id("acme", "b/0") != globex[0]["job_id"] == job_id("globex", "b/0")
    )
    assert acme[-1]["summary"]["batch_id"] != globex[-1]["summary"]["batch_id"]
    rows = db.execute("SELECT tenant, batch_id::text FROM accepted_job ORDER BY tenant").fetchall()
    assert rows == [("acme", batch_id("acme", "b")), ("globex", batch_id("globex", "b"))]
    with pytest.raises(Refused, match="^UNKNOWN_TENANT: "):
        call(submit, [header(template), entry(0)], tenant="initech")


def test_invalid_template_is_refused_before_any_answer(make_submit, call, template, db):
    template["tasks"]["build"]["params"]["source"]["sha256"] = "0" * 64
    with pytest.raises(Refused) as refused:
        call(make_submit(), [header(template), entry(0)])
    assert (refused.value.code, refused.value.http) == ("INVALID_TEMPLATE", 400)
    assert counts(db) == (0, 0)


@pytest.mark.slow
def test_submit_10000(route, post, entries, template, db):
    for outcome in ("accepted", "duplicate"):
        status, answers = post(route, "acme", [header(template), *entries(10_000)])
        assert status == 200
        tally, summary = {}, None
        for answer in answers:
            if "summary" in answer:
                summary = answer["summary"]
            else:
                tally[answer["outcome"]] = tally.get(answer["outcome"], 0) + 1
        assert tally == {outcome: 10_000}
        assert summary[outcome] == 10_000
        assert counts(db) == (10_000, 10_000)
