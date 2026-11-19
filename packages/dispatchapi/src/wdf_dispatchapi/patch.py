"""The resume route of one job, with an optional patched template.

A resume without a template is a control call as any other, and clears the pending patch. A patched
resume of a job whose workflow runs is checked before anything is written: the template is
validated and compiled as a batch template is, and the patch check is run against the plan in
force, which is the pending patch when one is set and else the batch manifest, bound to the job's
params, with the completed tasks of the workflow's status query. The workflow runs the same check
when it loads the patch, and its verdict is the one that decides. Only then is the patched manifest
stored, indexed and linked to the job, and the resume requested with it in one control transaction.

A call that the control table refuses or answers as a noop stores nothing: the table is asked on
the row as read before the check, and again on the locked row in the control transaction.
"""

import json
import re

from psycopg import Connection
from wdf_artifactclient.client import ArtifactClient
from wdf_artifactclient.errors import ArtifactError
from wdf_events.models import UUID_PATTERN
from wdf_jobcheck import SourceResolver, validate
from wdf_jobpatch import patch_check
from wdf_jobstate import JobRow, get_job
from wdf_plan import Plan, bind, canonical_bytes, compile_plan, manifest_reference, read_plan
from wdf_plan.canonical import MANIFEST_NAME
from wdf_workflow.ready import DONE

from .control import ControlRefused, control_job
from .control_table import ASK_GATEWAY, decide
from .models import ControlAnswer, ControlRequest
from .ports import NoWorkflow, TemporalUnavailable, WorkflowGateway


class Resume:
    """The resume route over a store client and a resolver of the `submit` identity, and the
    limits profile that checks a template."""

    def __init__(self, store: ArtifactClient, resolver: SourceResolver, profile):
        self.store, self.resolver, self.profile = store, resolver, profile

    def call(
        self,
        control: Connection,
        artifacts: Connection,
        gateway: WorkflowGateway,
        tenant: str,
        job_id: str,
        request: ControlRequest,
    ) -> ControlAnswer:
        """Resumes one job, on a connection of the control login and one of the artifacts login,
        both in autocommit mode. Raises ControlRefused."""
        if request.template is None:
            return control_job(control, gateway, tenant, job_id, "resume", request)
        try:
            return self._patched(control, artifacts, gateway, tenant, job_id, request)
        except ArtifactError as error:
            raise ControlRefused("STORE_UNAVAILABLE", str(error)) from error

    def _patched(self, control, artifacts, gateway, tenant, job_id, request) -> ControlAnswer:
        row = get_job(control, job_id) if re.fullmatch(UUID_PATTERN, job_id) else None
        if row is None or row.tenant != tenant:
            raise ControlRefused("JOB_NOT_FOUND", f"tenant {tenant} has no job {job_id}")
        plan = self._compiled(request.template)
        patch = manifest_reference(plan)
        if not _waits_for_the_workflow(gateway, row, patch):
            return control_job(control, gateway, tenant, job_id, "resume", request, patch=patch)
        self._check(gateway, row, plan)
        with artifacts.transaction():
            stored = self.store.put_content(
                canonical_bytes(plan), "manifest", MANIFEST_NAME, conn=artifacts, job_id=job_id
            )
        patch = stored.model_dump(mode="json")
        return control_job(control, gateway, tenant, job_id, "resume", request, patch=patch)

    def _compiled(self, template) -> Plan:
        report = validate(json.dumps(template).encode(), self.profile, self.resolver)
        if not report.ok:
            raise ControlRefused("PATCH_INVALID", _issues(report.issues))
        return compile_plan(report.document, self.profile)

    def _check(self, gateway: WorkflowGateway, row: JobRow, plan: Plan) -> None:
        """Runs the patch check against the plan in force, with the tasks the workflow completed."""
        try:
            tasks = gateway.status(row.job_id).tasks
        except TemporalUnavailable as error:
            raise ControlRefused("TEMPORAL_UNAVAILABLE", str(error)) from error
        except NoWorkflow as error:
            raise ControlRefused(
                "PATCH_NOT_STARTED", f"job {row.job_id} has no workflow"
            ) from error
        in_force = row.pending_patch_ref or row.manifest_ref
        old = read_plan(self.store.get_bytes(in_force, self.profile.max_plan_bytes))
        params = {"seed": row.seed, "parameters": row.parameters}
        try:
            new = bind(plan, params, self.profile)
        except ValueError as error:
            raise ControlRefused(
                "PATCH_INCOMPATIBLE", f"the job's params do not bind: {error}"
            ) from error
        completed = {task.id for task in tasks if task.state in DONE}
        report = patch_check(bind(old, params, self.profile), new, completed)
        if not report.compatible:
            raise ControlRefused("PATCH_INCOMPATIBLE", _issues(report.issues))


def _waits_for_the_workflow(gateway: WorkflowGateway, row: JobRow, patch: dict) -> bool:
    """Whether the control table, asked on the row as read, requests the resume of a job whose
    workflow takes the patch. Every other outcome is decided again under the lock."""
    try:
        has_workflow = gateway.exists(row.job_id) if row.status in ASK_GATEWAY else None
    except TemporalUnavailable:
        has_workflow = None
    pending = row.pending_patch_ref
    same = pending is not None and pending["sha256"] == patch["sha256"]
    decision = decide(
        row.status, has_workflow, row.requested_action, "resume", "same" if same else "other"
    )
    return decision.outcome == "requested"


def _issues(issues) -> str:
    return "; ".join(f"{issue.path or '/'} {issue.code}: {issue.message}" for issue in issues)
