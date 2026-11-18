"""The view that an outstanding action shows over a stored status."""

from typing import get_args

from wdf_dispatchapi.models import Action, Status
from wdf_dispatchapi.reads import view

TERMINAL = {"succeeded", "partial", "failed", "cancelled", "terminated"}


def expected(status: str, action: str | None) -> str | None:
    if action == "pause" and status in ("admitted", "running"):
        return "pausing"
    if action == "cancel" and status not in TERMINAL:
        return "cancelling"
    return None


def test_view_from_requested_action():
    for status in get_args(Status):
        for action in (None, *get_args(Action)):
            assert view(status, action) == expected(status, action), (status, action)
