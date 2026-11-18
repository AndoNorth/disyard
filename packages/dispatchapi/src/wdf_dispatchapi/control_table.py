"""The control table as a pure function: what one action does to a job, decided on the row that
control read with `lock_job`, before any write. `contracts/api/control.yaml` is the source, and
a test holds this function equal to it.

The workflow is known by the status for a job at `accepted` (none), `admitted` or `running` (one),
and the gateway is asked for a job at `paused` or `resume-pending`. A job whose gateway did not
answer is decided with `has_workflow` None: a `cancel` or `terminate` is requested for the
retrier to finish, and a `resume` is refused `TEMPORAL_UNAVAILABLE`.
"""

from dataclasses import dataclass
from typing import Literal

from .models import Action, Move, Status, View

Outcome = Literal["applied", "requested", "noop", "refused"]
# none: no patch sent; same or other: than the pending patch, compared by manifest hash.
Patch = Literal["none", "same", "other"]

TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled", "terminated"})
ASK_GATEWAY = frozenset({"paused", "resume-pending"})
_WORKFLOW_BY_STATUS = {"accepted": False, "admitted": True, "running": True}
_ENDED = {"cancel": "cancelled", "terminate": "terminated", "pause": "paused"}


@dataclass(frozen=True)
class Decision:
    """What control writes, in one transaction: `request_action` when `request`, then each move
    in order, then `pending_patch_ref` set to the sent patch or cleared when `set_patch`, then
    each event, with ids by the control stage's rule."""

    outcome: Outcome
    refusal: str | None = None
    request: bool = False
    moves: tuple[Move, ...] = ()
    set_patch: bool = False
    events: tuple[str, ...] = ()


NOOP = Decision("noop")


def _refused(code: str) -> Decision:
    return Decision("refused", code)


# What an action does while another is outstanding. A pair that is absent goes to the cells.
_OVERLAY: dict[tuple[Action, Action], Decision] = {
    ("terminate", "cancel"): NOOP,
    ("terminate", "terminate"): NOOP,
    ("terminate", "pause"): _refused("STOPPING"),
    ("terminate", "resume"): _refused("STOPPING"),
    ("cancel", "cancel"): NOOP,
    ("cancel", "pause"): _refused("STOPPING"),
    ("cancel", "resume"): _refused("STOPPING"),
    ("pause", "pause"): NOOP,
    ("pause", "resume"): _refused("PAUSE_IN_PROGRESS"),
}


def decide(
    status: Status,
    has_workflow: bool | None,
    requested_action: Action | None,
    action: Action,
    patch: Patch = "none",
) -> Decision:
    """What `action` does to a job at `status` with `requested_action` outstanding.

    Raises ValueError for a patch on an action other than resume, and for a `has_workflow` that
    contradicts the status.
    """
    if action != "resume" and patch != "none":
        raise ValueError(f"only a resume carries a patch, not a {action}")
    if status in _WORKFLOW_BY_STATUS:
        known = _WORKFLOW_BY_STATUS[status]
        if has_workflow not in (None, known):
            raise ValueError(f"a job at {status} has {'a' if known else 'no'} workflow")
        has_workflow = known
    if requested_action is not None and (requested_action, action) in _OVERLAY:
        return _OVERLAY[requested_action, action]
    if status in TERMINAL:
        return NOOP if action in ("cancel", "terminate") else _refused("ALREADY_ENDED")
    if action == "resume":
        return _resume(status, has_workflow, patch)
    if action == "pause" and status == "paused":
        return NOOP
    if action == "pause" and status == "resume-pending":
        return _refused("RESUME_PENDING")
    requested = f"job.{action}.requested"
    if has_workflow is not False:
        return Decision("requested", request=True, events=(requested,))
    ended = _ENDED[action]
    return Decision(
        "applied",
        request=True,
        moves=(Move(to=ended, row_only=status == "accepted"),),
        events=(requested, f"job.{ended}"),
    )


def _resume(status: Status, has_workflow: bool | None, patch: Patch) -> Decision:
    if status not in ASK_GATEWAY:
        return _refused("NOT_PAUSED")
    if has_workflow is None:
        return _refused("TEMPORAL_UNAVAILABLE")
    if not has_workflow:
        if patch != "none":
            return _refused("PATCH_NOT_STARTED")
        if status == "resume-pending":
            return NOOP
        return Decision(
            "applied",
            request=True,
            moves=(Move(to="resume-pending"), Move(to="accepted", row_only=True)),
            events=("job.resume.requested", "job.resumed"),
        )
    if status == "paused":
        return Decision(
            "requested",
            request=True,
            moves=(Move(to="resume-pending"),),
            set_patch=True,
            events=("job.resume.requested",),
        )
    return _refused("PATCH_PENDING") if patch == "other" else NOOP


def view(status: Status, requested_action: Action | None) -> View | None:
    """The view that an outstanding action shows over the status, never a stored status."""
    if requested_action == "pause" and status in ("admitted", "running"):
        return "pausing"
    if requested_action == "cancel" and status not in TERMINAL:
        return "cancelling"
    return None
