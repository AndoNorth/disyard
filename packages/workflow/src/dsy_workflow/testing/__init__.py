"""Stubs and fakes that stand in for the placement service, the pool workers and the control
activities, with the conformance tests that the real ones pass."""

from .builders import FAN, accepted_event, plan
from .control import FakeControl, control_activity_contract
from .placer import placer_contract
from .pool import (
    ContractCases,
    Die,
    Fail,
    FakePool,
    Hang,
    Interruptible,
    Succeed,
    Zombie,
    activity_contract,
    run_task_once,
)
from .rig import Rig, control_worker, rig
from .server import build_id, client, connect, dev_server, register_search_attributes

__all__ = [
    "FAN",
    "Rig",
    "accepted_event",
    "build_id",
    "client",
    "control_worker",
    "dev_server",
    "plan",
    "register_search_attributes",
    "rig",
    "ContractCases",
    "Die",
    "Fail",
    "FakeControl",
    "FakePool",
    "Hang",
    "Interruptible",
    "Succeed",
    "Zombie",
    "activity_contract",
    "connect",
    "control_activity_contract",
    "placer_contract",
    "run_task_once",
]
