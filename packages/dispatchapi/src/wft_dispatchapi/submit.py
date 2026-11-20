"""The batch submit route: a header with one template, then up to the entry limit of jobs, each
answered once its row and its accepted event are durable.

The template is validated, compiled and stored as the batch manifest once per call, and each object
it names is indexed once. Then each chunk of entries runs the same sequence: check each entry, link
every object to each job on the artifacts login and commit, insert the rows and one `job.accepted`
per inserted row on the dispatch login and commit, and answer each entry. A link exists before its
row, so the reaper never sees a job whose objects it may take. Then the chunk's new rows are
published, so the answer is bounded by Postgres and never by the broker, until a publish ends the
run, after which the call publishes nothing more and leaves its rows to the reconciler.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from wft_artifactclient.client import ArtifactClient
from wft_artifactclient.errors import ArtifactError, ArtifactNotFound
from wft_artifactindex import RowPurged, index_reference, link_use
from wft_broker.models import TenancyProfile
from wft_events import Event, build_event
from wft_events.models import ContentReference
from wft_eventstore import insert_events
from wft_jobcheck import SourceResolver, validate
from wft_jobstate import Duplicate, JobRow, insert_accepted
from wft_plan import canonical_bytes, check_job_params, compile_plan, manifest_reference
from wft_plan.canonical import MANIFEST_NAME
from wft_temporal import new_traceparent, trace_id_of

from .keys import Identity, batch_id, identify, job_key
from .limits import CallSlots, Limits, TooManyCalls
from .models import (
    ENTRIES_MAX,
    ISSUES_MAX,
    BatchEntry,
    BatchHeader,
    EntryAnswer,
    ErrorBody,
    ErrorLine,
    SubmitSummary,
    SummaryLine,
    ValidatorIssue,
)
from .ndjson import Reader, Rejected, Stop, Writer
from .objects import Refused

SOURCE = "/wft/dispatch"
SCHEMA_VERSION = "urn:wft:schema:job.accepted:1.0"


def accepted_event(row: JobRow, time: datetime) -> Event:
    """The `job.accepted` event of a row. Built with the row's `created_at` it is the stored event,
    byte for byte, because the submit builds it with the time of the transaction that inserts the
    row."""
    return build_event(
        "job.accepted",
        source=SOURCE,
        tenant=row.tenant,
        jobid=row.job_id,
        priorityclass=row.priority_class,
        jobclass=row.job_class,
        traceparent=row.traceparent,
        tracestate=row.tracestate,
        time=time,
        data={
            "manifest": row.manifest_ref,
            "batch_id": row.batch_id,
            "submission_key": row.submission_key,
        },
    )


class _Ended(Exception):
    """The call stops with a reason code, and nothing of the chunk in hand was written."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class _Gone(Exception):
    """An answer write failed, so no later line can reach the client."""


@dataclass(frozen=True)
class _Job:
    index: int
    identity: Identity
    row: JobRow


class Submit:
    """The submit route over a store client and a resolver of the `submit` identity, the limits
    profile that checks a template and the tenancy profile that names tenants and classes, and the
    publisher of the accepted events, without which every row waits for the reconciler."""

    def __init__(
        self,
        store: ArtifactClient,
        resolver: SourceResolver,
        profile,
        tenancy: TenancyProfile,
        limits: Limits | None = None,
        publisher=None,
    ):
        self.store, self.resolver, self.profile, self.tenancy = store, resolver, profile, tenancy
        self.limits = limits or Limits()
        self.publisher = publisher
        self.slots = CallSlots(self.limits)

    def call(
        self,
        tenant: str,
        reader: Reader,
        dispatch: psycopg.Connection,
        artifacts: psycopg.Connection,
        begin: Callable[[], Writer],
        trace_id: str | None = None,
    ) -> SubmitSummary | None:
        """Runs one call on two connections in autocommit mode: one of the dispatch login and one of
        the artifacts login.

        `begin` sends the answer's status line and headers and returns its writer, and is called
        before the first answer line. A call refused before that raises Refused, with nothing
        written. Returns the summary, or None when the call stopped with an ErrorLine or lost its
        client.
        """
        if tenant not in self.tenancy.tenants:
            raise Refused("UNKNOWN_TENANT", f"{tenant} is not a tenant of the configuration")
        try:
            with self.slots.hold(tenant):
                call = _Call(self, tenant, dispatch, artifacts, begin, trace_id)
                return call.run(reader)
        except TooManyCalls as error:
            raise Refused("TOO_MANY_CALLS", str(error)) from None


class _Call:
    def __init__(self, submit: Submit, tenant, dispatch, artifacts, begin, trace_id):
        self.submit, self.tenant = submit, tenant
        self.dispatch, self.artifacts, self.begin = dispatch, artifacts, begin
        self.trace_id = trace_id or trace_id_of(new_traceparent())
        self.writer: Writer | None = None
        # One bit per index, so the seen indexes of the largest call take 12.5 KiB.
        self.seen = bytearray(ENTRIES_MAX // 8 + 1)
        self.counts = {"accepted": 0, "duplicate": 0, "rejected": 0}
        self.manifest_retried = False
        self.publishing = submit.publisher is not None

    def run(self, reader: Reader) -> SubmitSummary | None:
        try:
            header = reader.header()
            if isinstance(header, Stop):
                raise _Ended(header.code, header.message)
            self.header = header
            self.batch_id = batch_id(self.tenant, header)
            self._template(header)
            stop = self._entries(reader)
            if stop is not None:
                raise _Ended(stop.code, stop.message)
            summary = SubmitSummary(batch_id=self.batch_id, **self.counts)
            self._write(SummaryLine(summary=summary))
            self._end()
            return summary
        except _Ended as ended:
            self._stop(ended.code, ended.message)
        except psycopg.OperationalError as error:
            self._stop("DATABASE_UNAVAILABLE", str(error))
        except ArtifactError as error:
            self._stop("STORE_UNAVAILABLE", str(error))
        except _Gone:
            pass
        return None

    def _stop(self, code: str, message: str) -> None:
        """Refuses the call while nothing is answered and the code has an HTTP status, and ends the
        answer with an ErrorLine otherwise."""
        if self.writer is None and Refused(code, message).http is not None:
            raise Refused(code, message)
        try:
            self._write(
                ErrorLine(error=ErrorBody(code=code, message=message, trace_id=self.trace_id))
            )
            self._end()
        except _Gone:
            pass

    def _write(self, *lines) -> None:
        try:
            if self.writer is None:
                self.writer = self.begin()
            self.writer.write(*lines)
        except OSError as error:
            raise _Gone() from error

    def _end(self) -> None:
        try:
            self.writer.end()
        except OSError as error:
            raise _Gone() from error

    def _template(self, header: BatchHeader) -> None:
        """Validates and compiles the template, then indexes the manifest and every object the
        template names and stores the manifest under the index row's lock, as `put_content` does
        with a connection, so a row revived here never lacks its object."""
        report = validate(
            json.dumps(header.template).encode(), self.submit.profile, self.submit.resolver
        )
        if not report.ok:
            issues = "; ".join(f"{i.path or '/'} {i.code}: {i.message}" for i in report.issues)
            raise _Ended("INVALID_TEMPLATE", issues)
        self.plan = compile_plan(report.document, self.submit.profile)
        self.manifest = manifest_reference(self.plan)
        uploaded = [
            ContentReference.model_validate(ref.model_dump()) for ref in _named(report.document)
        ]
        self.uris = [self.manifest["uri"]] + [ref.uri for ref in uploaded]
        with self.artifacts.transaction():
            for ref in [self.manifest, *(ref.model_dump(mode="json") for ref in uploaded)]:
                index_reference(self.artifacts, ref)
            self._put_manifest()
            for ref in uploaded:
                try:
                    self.submit.store.head(ref)
                except ArtifactNotFound:
                    raise _Ended("OBJECT_MISSING", f"{ref.uri} is no longer stored") from None

    def _put_manifest(self) -> None:
        self.submit.store.put_content(canonical_bytes(self.plan), "manifest", MANIFEST_NAME)

    def _entries(self, reader: Reader) -> Stop | None:
        chunk: list[BatchEntry | Rejected] = []
        for item in reader.entries():
            if isinstance(item, Stop):
                if chunk:
                    self._chunk(chunk)
                return item
            chunk.append(item)
            if len(chunk) == self.submit.limits.chunk_entries:
                self._chunk(chunk)
                chunk = []
        if chunk:
            self._chunk(chunk)
        return None

    def _chunk(self, items: list[BatchEntry | Rejected]) -> None:
        answers: list[EntryAnswer | None] = []
        jobs: dict[int, _Job] = {}
        self.accepted: list[Event] = []
        for item in items:
            checked = self._check(item)
            if isinstance(checked, _Job):
                jobs[len(answers)] = checked
                answers.append(None)
            else:
                answers.append(checked)
        if jobs:
            self._link(jobs.values())
            for position, answer in zip(jobs, self._insert(list(jobs.values())), strict=True):
                answers[position] = answer
        for answer in answers:
            self.counts[answer.outcome] += 1
        self._write(*answers)
        if self.publishing and self.accepted:
            self.publishing = self.submit.publisher.publish(self.dispatch, self.accepted)

    def _check(self, item: BatchEntry | Rejected) -> _Job | EntryAnswer:
        if isinstance(item, Rejected):
            return _rejected(item.reason, item.index)
        header, submit = self.header, self.submit
        identity = identify(self.tenant, header, item, self.manifest["sha256"])
        job_id = identity.job_id
        byte, bit = divmod(item.index, 8)
        if self.seen[byte] & (1 << bit):
            return _rejected("DUPLICATE_INDEX", item.index, job_id)
        self.seen[byte] |= 1 << bit
        if identity.priority_class not in submit.tenancy.priority_classes:
            return _rejected("UNKNOWN_PRIORITY_CLASS", item.index, job_id)
        if identity.job_class not in submit.profile.classes:
            return _rejected("UNKNOWN_JOB_CLASS", item.index, job_id)
        params = {"seed": item.seed, "parameters": item.parameters}
        if issues := check_job_params(self.plan, params, submit.profile):
            found = [ValidatorIssue(path=i.path, code=i.code) for i in issues[:ISSUES_MAX]]
            return EntryAnswer(
                index=item.index,
                outcome="rejected",
                job_id=job_id,
                reason="INVALID_PARAMS",
                issues=found,
            )
        row = JobRow(
            job_id=identity.job_id,
            tenant=self.tenant,
            submission_key=job_key(header.batch_key, item),
            batch_id=self.batch_id,
            priority_class=identity.priority_class,
            job_class=identity.job_class,
            manifest_ref=self.manifest,
            schema_version=SCHEMA_VERSION,
            traceparent=new_traceparent(),
            seed=item.seed,
            parameters=item.parameters,
            fingerprint=identity.fingerprint,
        )
        return _Job(item.index, identity, row)

    def _link(self, jobs) -> None:
        """Links every object of the call to each job, renewing a link a resend made before."""
        at = datetime.now(UTC)
        with self.artifacts.transaction():
            for job in jobs:
                for uri in self.uris:
                    if isinstance(link_use(self.artifacts, job.row.job_id, uri, at), RowPurged):
                        self._relink(job.row.job_id, uri, at)

    def _relink(self, job_id: str, uri: str, at: datetime) -> None:
        """Stores the manifest again once per call when the reaper purged it, and stops the call
        when another object was purged, because only its uploader holds its bytes."""
        if uri != self.manifest["uri"] or self.manifest_retried:
            raise _Ended("OBJECT_MISSING", f"{uri} was purged, so it must be uploaded again")
        self.manifest_retried = True
        index_reference(self.artifacts, self.manifest)
        self._put_manifest()
        if isinstance(link_use(self.artifacts, job_id, uri, at), RowPurged):
            raise _Ended("OBJECT_MISSING", f"{uri} was purged again while it was stored")

    def _insert(self, jobs: list[_Job]) -> list[EntryAnswer]:
        """Inserts the rows and the accepted event of each new one in one transaction, and answers
        each job."""
        rows = [job.row for job in jobs]
        with self.dispatch.transaction():
            now = self.dispatch.execute("SELECT now()").fetchone()[0]
            result = insert_accepted(self.dispatch, rows)
            # The rows come back in order, each inserted or a duplicate, so walking both lists in
            # step pairs each job with its own outcome, also when two entries share a key.
            inserted, duplicates = iter(result.inserted), iter(result.duplicates)
            won, existing = next(inserted, None), []
            for job in jobs:
                if job.row.job_id == won:
                    existing.append(None)
                    won = next(inserted, None)
                else:
                    existing.append(next(duplicates))
            new = [job for job, found in zip(jobs, existing, strict=True) if found is None]
            self.accepted = [accepted_event(job.row, now) for job in new]
            insert_events(self.dispatch, self.accepted)
            traces = self._traces([found.job_id for found in existing if found is not None])
        return [self._answer(job, found, traces) for job, found in zip(jobs, existing, strict=True)]

    @staticmethod
    def _answer(job: _Job, found: Duplicate | None, traces: dict[str, str]) -> EntryAnswer:
        if found is None:
            trace = trace_id_of(job.row.traceparent)
            return EntryAnswer(
                index=job.index, outcome="accepted", job_id=job.row.job_id, trace_id=trace
            )
        if found.fingerprint != job.identity.fingerprint:
            return _rejected("KEY_CONFLICT", job.index, found.job_id)
        return EntryAnswer(
            index=job.index,
            outcome="duplicate",
            job_id=found.job_id,
            status=found.status,
            trace_id=traces[found.job_id],
        )

    def _traces(self, job_ids: list[str]) -> dict[str, str]:
        """The trace id of each existing job, which a duplicate answers with."""
        if not job_ids:
            return {}
        rows = self.dispatch.execute(
            "SELECT job_id::text, traceparent FROM accepted_job WHERE job_id = ANY(%s::uuid[])",
            (job_ids,),
        ).fetchall()
        return {job_id: trace_id_of(traceparent) for job_id, traceparent in rows}


def _rejected(reason: str, index: int | None, job_id: str | None = None) -> EntryAnswer:
    fields = {"index": index, "job_id": job_id}
    return EntryAnswer(
        outcome="rejected", reason=reason, **{k: v for k, v in fields.items() if v is not None}
    )


def _named(document) -> list:
    """The uploaded objects a template names: each compile task's source bundle and each aggregate
    task's index object."""
    named = {}
    for task in document.tasks.values():
        if task.type == "compile":
            named[task.params.source.uri] = task.params.source
        elif task.type == "aggregate" and task.params.index is not None:
            named[task.params.index.uri] = task.params.index
    return list(named.values())
