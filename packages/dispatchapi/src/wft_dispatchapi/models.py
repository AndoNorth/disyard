"""The models of the API contract and the source of its generated schemas: the body of every
route, the route table, the reason table, the control table and the fingerprint vectors."""

from importlib import resources
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)
from wft_events.models import UUID_PATTERN, ContentReference, ResultReference
from wft_workflow.models import CONTROL_SEQ_MAX, REASON_MAX, StatusQuery

# The contract files the package reads ship inside it, as links to the repository files.
CONTRACT = resources.files(__package__) / "contract"

_STATUS = yaml.safe_load((CONTRACT / "status.yaml").read_text())
Status = Literal[tuple(_STATUS["statuses"])]
View = Literal[tuple(_STATUS["views"])]

ENTRIES_MAX = 100_000
ENTRY_LINE_MAX_BYTES = 2_048
# The 1 MiB max_document_bytes of the template plus 4 KiB for the rest of the header.
HEADER_LINE_MAX_BYTES = 1_052_672
SEED_MAX = 2**53 - 1
PAGE_MAX = 1_000
ISSUES_MAX = 10

CODE = "^[A-Z][A-Z0-9_]*$"
SHA256 = "^[0-9a-f]{64}$"
TRACE_ID = "^[0-9a-f]{32}$"
ISSUE_CODE = "^[a-z][a-z0-9-]*$"

Action = Literal["cancel", "terminate", "pause", "resume"]
Count = Annotated[StrictInt, Field(ge=0)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# Request and answer bodies.


class BatchHeader(_Model):
    """The first line of a batch submit call."""

    batch_key: StrictStr = Field(min_length=1, max_length=200)
    template: dict[str, Any] = Field(description="The job template document, once per batch.")
    priority_class: StrictStr = Field("standard", min_length=1)
    job_class: StrictStr = Field("short", min_length=1)


class BatchEntry(_Model):
    """One job of a batch submit call, one line each after the header."""

    index: StrictInt = Field(ge=0, lt=ENTRIES_MAX, description="Unique within the call.")
    submission_key: StrictStr | None = Field(
        None, min_length=1, max_length=256, description="Defaults to <batch key>/<index>."
    )
    seed: StrictInt = Field(ge=0, le=SEED_MAX)
    parameters: dict[str, Any]
    priority_class: StrictStr | None = Field(None, min_length=1)
    job_class: StrictStr | None = Field(None, min_length=1)


class ValidatorIssue(_Model):
    path: StrictStr = Field(description="A JSON Pointer into the entry.")
    code: StrictStr = Field(pattern=ISSUE_CODE)


class EntryAnswer(_Model):
    """The answer to one entry, in request order."""

    index: StrictInt | None = Field(
        None, ge=0, lt=ENTRIES_MAX, description="Absent when the line did not parse that far."
    )
    outcome: Literal["accepted", "duplicate", "rejected"]
    job_id: StrictStr | None = Field(
        None, pattern=UUID_PATTERN, description="Absent when the line did not parse that far."
    )
    status: Status | None = Field(None, description="The existing row's status, on duplicate.")
    reason: StrictStr | None = Field(None, pattern=CODE, description="On rejected only.")
    issues: list[ValidatorIssue] | None = Field(
        None, min_length=1, max_length=ISSUES_MAX, description="On INVALID_PARAMS only."
    )
    trace_id: StrictStr | None = Field(None, pattern=TRACE_ID)

    @model_validator(mode="after")
    def _by_outcome(self):
        rejected = self.outcome == "rejected"
        if rejected != (self.reason is not None):
            raise ValueError("a reason is given on rejected and only there")
        if (self.outcome == "duplicate") != (self.status is not None):
            raise ValueError("a status is given on duplicate and only there")
        if self.issues is not None and self.reason != "INVALID_PARAMS":
            raise ValueError("issues are given on INVALID_PARAMS only")
        if not rejected and None in (self.index, self.job_id, self.trace_id):
            raise ValueError("an accepted or duplicate entry has its index, job id and trace id")
        return self


class SubmitSummary(_Model):
    batch_id: StrictStr = Field(pattern=UUID_PATTERN)
    accepted: Count
    duplicate: Count
    rejected: Count


class ControlSummary(_Model):
    applied: Count
    requested: Count
    noop: Count
    refused: Count


class SummaryLine(_Model):
    """The last line of a batch submit or a batch control call that ran to its end."""

    summary: SubmitSummary | ControlSummary


class ErrorBody(_Model):
    """Every error answer, and the error of an ErrorLine."""

    code: StrictStr = Field(pattern=CODE)
    message: StrictStr
    trace_id: StrictStr = Field(pattern=TRACE_ID)


class ErrorLine(_Model):
    """The last line of a call that stopped after it began answering."""

    error: ErrorBody


class EntryError(_Model):
    """One line of a batch control answer, for a job whose action was refused."""

    job_id: StrictStr = Field(pattern=UUID_PATTERN)
    submission_key: StrictStr = Field(min_length=1, max_length=256)
    code: StrictStr = Field(pattern=CODE)
    message: StrictStr


class ControlRequest(_Model):
    reason: StrictStr | None = Field(None, min_length=1, max_length=REASON_MAX)
    template: dict[str, Any] | None = Field(None, description="The patched template, on resume.")


class ControlAnswer(_Model):
    job_id: StrictStr = Field(pattern=UUID_PATTERN)
    action: Action
    outcome: Literal["applied", "requested", "noop"]
    status: Status
    view: View | None
    control_seq: StrictInt = Field(ge=0, le=CONTROL_SEQ_MAX)


class DeadLetter(_Model):
    dlq_reason: StrictStr = Field(pattern=CODE)
    detail: StrictStr
    time: AwareDatetime


class JobStatus(_Model):
    job_id: StrictStr = Field(pattern=UUID_PATTERN)
    tenant: StrictStr = Field(min_length=1)
    batch_id: StrictStr = Field(pattern=UUID_PATTERN)
    submission_key: StrictStr = Field(min_length=1, max_length=256)
    status: Status
    view: View | None
    requested_action: Action | None
    control_seq: StrictInt = Field(ge=0, le=CONTROL_SEQ_MAX)
    control_delivered: StrictBool
    priority_class: StrictStr = Field(min_length=1)
    job_class: StrictStr = Field(min_length=1)
    published: StrictBool
    created_at: AwareDatetime
    updated_at: AwareDatetime
    terminal_at: AwareDatetime | None
    trace_id: StrictStr = Field(pattern=TRACE_ID)
    detail: StatusQuery | None
    detail_error: Literal["unavailable", "no-workflow"] | None
    results: list[ResultReference] = Field(description="In task id order.")
    dead_letter: DeadLetter | None

    @model_validator(mode="after")
    def _one_detail(self):
        if self.detail is not None and self.detail_error is not None:
            raise ValueError("detail and detail_error are never both set")
        return self


class ObjectAnswer(_Model):
    reference: ContentReference
    created: StrictBool

    @model_validator(mode="after")
    def _uploaded_kind(self):
        if self.reference.kind not in ("source", "index"):
            raise ValueError("an upload is a source or an index object")
        return self


class BatchSummary(_Model):
    batch_id: StrictStr = Field(pattern=UUID_PATTERN)
    total: Count
    counts: dict[Status, Count] = Field(description="Jobs per status, statuses with none left out.")


class JobLine(_Model):
    job_id: StrictStr = Field(pattern=UUID_PATTERN)
    submission_key: StrictStr = Field(min_length=1, max_length=256)
    status: Status
    view: View | None


class JobPage(_Model):
    jobs: list[JobLine] = Field(max_length=PAGE_MAX, description="In job id order.")
    next: StrictStr | None = Field(
        pattern=UUID_PATTERN, description="The `after` of the next page."
    )


class Depth(_Model):
    depth: dict[StrictStr, dict[StrictStr, Count]] = Field(
        description="Rows at accepted, keyed by priority class then job class."
    )


class Health(_Model):
    status: Literal["ok", "ready", "not-ready"]


# The contract files.

Listener = Literal["dispatch-api", "control-api"]
BODIES = {
    "batch-header": BatchHeader,
    "batch-entry": BatchEntry,
    "entry-answer": EntryAnswer,
    "summary-line": SummaryLine,
    "error-line": ErrorLine,
    "entry-error": EntryError,
    "error": ErrorBody,
    "control-request": ControlRequest,
    "control-answer": ControlAnswer,
    "job-status": JobStatus,
    "object-answer": ObjectAnswer,
    "batch-summary": BatchSummary,
    "job-page": JobPage,
    "depth": Depth,
    "health": Health,
}
BodyName = Literal[tuple(BODIES)]


class Payload(_Model):
    body: Literal["none", "json", "ndjson", "bytes"]
    models: list[BodyName] = Field([], description="The line models, in order, of an NDJSON body.")

    @model_validator(mode="after")
    def _models_by_body(self):
        counts = {"none": (0, 0), "bytes": (0, 0), "json": (1, 1), "ndjson": (1, len(BODIES))}
        low, high = counts[self.body]
        if not low <= len(self.models) <= high:
            raise ValueError(f"a {self.body} body names {low} to {high} models")
        return self


class QueryParam(_Model):
    name: StrictStr = Field(pattern="^[a-z_]+$")
    type: Literal["status", "job-id", "integer"]
    minimum: StrictInt | None = None
    maximum: StrictInt | None = None


class Route(_Model):
    listeners: list[Listener] = Field(min_length=1, max_length=2)
    method: Literal["GET", "PUT", "POST"]
    path: StrictStr = Field(pattern="^/[a-z0-9{}_/-]*$")
    values: dict[StrictStr, list[StrictStr]] = Field(
        {}, description="The values a path parameter may take, where they are a fixed set."
    )
    query: list[QueryParam] = []
    request: Payload
    answer: Payload
    success: list[StrictInt] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self):
        if len(set(self.listeners)) != len(self.listeners):
            raise ValueError("a listener is named twice")
        for name in self.values:
            if f"{{{name}}}" not in self.path:
                raise ValueError(f"{name} is not a parameter of {self.path}")
        if any(not 200 <= code < 300 for code in self.success):
            raise ValueError("a success status is a 2xx")
        return self


class Routes(_Model):
    routes: list[Route] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self):
        keys = [(r.method, r.path) for r in self.routes]
        if len(set(keys)) != len(keys):
            raise ValueError("two routes share a method and a path")
        return self


Scope = Literal["call", "entry", "upload", "control", "read", "any"]


class ReasonRow(_Model):
    code: StrictStr = Field(pattern=CODE)
    scopes: list[Scope] = Field(min_length=1)
    http: StrictInt | None = Field(
        ge=400, le=599, description="Null for a code carried only in an answer or error line."
    )
    when: StrictStr = Field(min_length=1)


class Reasons(_Model):
    reasons: list[ReasonRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self):
        codes = [r.code for r in self.reasons]
        if len(set(codes)) != len(codes):
            raise ValueError("a code is listed twice")
        return self


class Move(_Model):
    to: Status
    row_only: StrictBool = False


class Cell(_Model):
    """What one action does to a job in one state."""

    status: list[Status] = Field(min_length=1)
    workflow: Literal["with", "without", "either"]
    action: Action
    patch: list[Literal["none", "same", "other"]] = Field(
        ["none"],
        min_length=1,
        description="none: no patch sent; same or other: than the pending one.",
    )
    outcome: Literal["applied", "requested", "noop", "refused"]
    refusal: StrictStr | None = Field(None, pattern=CODE)
    request: StrictBool = Field(False, description="request_action(job_id, action) is called.")
    moves: list[Move] = Field([], description="Status moves in order, from the cell's status.")
    pending_patch: Literal["keep", "set-or-clear"] = "keep"
    events: list[StrictStr] = []

    @model_validator(mode="after")
    def _by_outcome(self):
        if (self.outcome == "refused") != (self.refusal is not None):
            raise ValueError("a refusal is given on refused and only there")
        writes = self.request or self.moves or self.events or self.pending_patch != "keep"
        if self.outcome in ("refused", "noop") and writes:
            raise ValueError(f"a {self.outcome} cell writes nothing")
        if self.outcome in ("applied", "requested") and not (self.request and self.events):
            raise ValueError(f"an {self.outcome} cell requests the action and records it")
        if self.outcome == "applied" and not self.moves:
            raise ValueError("an applied cell moves the row")
        if self.action != "resume" and self.patch != ["none"]:
            raise ValueError("only a resume carries a patch")
        return self


class OverlayRule(_Model):
    """What an action does while another is outstanding, decided before the cells."""

    outstanding: Action
    action: list[Action] = Field(min_length=1)
    rule: Literal["noop", "refused", "table"]
    refusal: StrictStr | None = Field(None, pattern=CODE)

    @model_validator(mode="after")
    def _refusal(self):
        if (self.rule == "refused") != (self.refusal is not None):
            raise ValueError("a refusal is given on refused and only there")
        return self


class ControlTable(_Model):
    cells: list[Cell] = Field(min_length=1)
    overlay: list[OverlayRule] = Field(min_length=1)


class FingerprintInput(_Model):
    job_class: StrictStr = Field(min_length=1)
    manifest_sha256: StrictStr = Field(pattern=SHA256)
    parameters: dict[str, Any]
    priority_class: StrictStr = Field(min_length=1)
    seed: StrictInt = Field(ge=0, le=SEED_MAX)


class FingerprintVector(_Model):
    name: StrictStr = Field(min_length=1)
    input: FingerprintInput
    canonical: StrictStr = Field(description="The canonical JSON of the input that is hashed.")
    output: StrictStr = Field(pattern=SHA256)


class Fingerprints(_Model):
    vectors: list[FingerprintVector] = Field(min_length=1)


# Each generated schema and the model it is generated from.
FILES: dict[str, type[BaseModel]] = {
    "routes": Routes,
    "reasons": Reasons,
    "control": ControlTable,
    "fingerprint": Fingerprints,
    **BODIES,
}


def schema_path(name: str) -> str:
    return f"contracts/api/schemas/{name}.schema.json"
