"""Loads one file of the broker or tenancy contract and checks it against the models and the event
contract, read through package data.

Usage: python -m dsy_broker.contract --check FILE [--as NAME]
NAME is a key of models.FILES. Without it, the name is the directory under vectors/ that holds
the file, or else the file's stem, so topology.yaml is checked as topology.
Exit 0 valid, 1 invalid with "reason: <code>: <detail>" on stderr, 2 usage.
"""

import argparse
import json
import re
import sys
from functools import cache
from pathlib import Path

import yaml
from dsy_events.profile import CONTRACT as EVENTS_CONTRACT
from dsy_events.profile import known_versions
from pydantic import BaseModel, ValidationError

from .models import CONTRACT, FILES, NAME_MAX_BYTES, PRESETS, Reasons, Tenancy, Topology

DEAD_LETTERED = "job.dead.lettered"
NAME = re.compile(r"[a-z][a-z0-9-]*(\.[a-z0-9-]+)*")
TEMPLATE = "intake.<tenant>.<class>"
USERS = ("dispatch", "admission", "operator")


class Refused(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@cache
def profile_patterns() -> dict[str, re.Pattern]:
    """The tenant, priority class and job class patterns of the event profile."""
    schema = json.loads((EVENTS_CONTRACT / "profile/profile.schema.json").read_text())
    return {
        key: re.compile(schema["properties"][key]["pattern"])
        for key in ("tenant", "priorityclass", "jobclass")
    }


@cache
def dead_letter_codes() -> set[str]:
    """The dlq_reason enum of the newest job.dead.lettered schema."""
    major, minor = known_versions(DEAD_LETTERED)[-1]
    path = EVENTS_CONTRACT / f"schemas/{DEAD_LETTERED}/{major}.{minor}.json"
    return set(json.loads(path.read_text())["properties"]["dlq_reason"]["enum"])


def sample_intake_queues() -> list[str]:
    """Intake queue names at the edges of the tenant and class patterns."""
    pairs = [("acme", "standard"), ("0", "0"), ("a-b", "c-d"), ("t" * 63, "c" * 32)]
    return [TEMPLATE.replace("<tenant>", t).replace("<class>", c) for t, c in pairs]


def intake_queue_name() -> re.Pattern:
    """Any name the template can produce from a tenant and a priority class."""
    patterns = profile_patterns()
    tenant, cls = (
        patterns[key].pattern.removeprefix("^").removesuffix("$")
        for key in ("tenant", "priorityclass")
    )
    return re.compile(re.escape("intake.") + f"(?:{tenant})" + re.escape(".") + f"(?:{cls})")


def _compiles(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _matches(pattern: str, name: str) -> bool:
    return re.search(pattern, name) is not None


def check_topology(model: Topology) -> None:
    names = [
        model.intake_exchange.name,
        model.dead_letter_exchange.name,
        model.dead_letter_queue.name,
        model.intake_policy.name,
        model.dlq_policy.name,
    ]
    for name in names:
        if not NAME.fullmatch(name) or len(name.encode()) > NAME_MAX_BYTES:
            raise Refused(
                "bad-name", f"{name!r} is not a dotted lower-case name of at most 255 bytes"
            )
    if model.intake_exchange.name == model.dead_letter_exchange.name:
        raise Refused("bad-name", "the intake and dead-letter exchanges share a name")
    if model.intake_policy.name == model.dlq_policy.name:
        raise Refused("bad-name", "the intake and dead-letter policies share a name")
    if model.intake_queue.template != TEMPLATE:
        raise Refused("bad-name", f"the intake queue template is {TEMPLATE!r}")
    for policy in (model.intake_policy, model.dlq_policy):
        if not _compiles(policy.pattern):
            raise Refused("bad-name", f"the pattern of {policy.name} is not a regular expression")
    dlq, intake = model.dead_letter_queue.name, sample_intake_queues()
    if any(not _matches(model.intake_policy.pattern, q) for q in intake):
        raise Refused("bad-name", f"{model.intake_policy.name} does not match every intake queue")
    if _matches(model.intake_policy.pattern, dlq):
        raise Refused("bad-name", f"{model.intake_policy.name} matches {dlq}")
    if not _matches(model.dlq_policy.pattern, dlq):
        raise Refused("bad-name", f"{model.dlq_policy.name} does not match {dlq}")
    if any(_matches(model.dlq_policy.pattern, q) for q in intake):
        raise Refused("bad-name", f"{model.dlq_policy.name} matches an intake queue")
    if intake_queue_name().fullmatch(dlq):
        raise Refused("bad-name", f"{dlq} could be the name of an intake queue")
    target = model.intake_policy.definition.dead_letter_exchange
    if target != model.dead_letter_exchange.name:
        raise Refused("bad-name", f"{model.intake_policy.name} dead-letters to {target}")
    if model.dlq_policy.definition.message_ttl is not None:
        raise Refused("dlq-ttl-set", f"{model.dlq_policy.name} sets a message TTL")
    users = model.users
    for name in USERS:
        permissions = getattr(users, name)
        for kind in ("configure", "write", "read"):
            if not _compiles(getattr(permissions, kind)):
                raise Refused(
                    "bad-user", f"the {kind} permission of {name} is not a regular expression"
                )
        if permissions.configure != "":
            raise Refused("bad-user", f"{name} may configure")
    if users.dispatch.read != "":
        raise Refused("bad-user", "dispatch may read")
    if users.admission.write != "":
        raise Refused("bad-user", "admission may write")


def check_reasons(model: Reasons) -> None:
    seen: set[str] = set()
    for row in model.reasons:
        if row.code in seen:
            raise Refused("duplicate-code", f"{row.code} is listed twice")
        seen.add(row.code)
    emitted = {row.code for row in model.reasons if row.event == DEAD_LETTERED}
    enum = dead_letter_codes()
    if emitted - enum:
        raise Refused(
            "event-code-mismatch",
            f"{', '.join(sorted(emitted - enum))} is not a dlq_reason of {DEAD_LETTERED}",
        )
    if enum - emitted:
        raise Refused(
            "event-code-mismatch",
            f"the dlq_reason {', '.join(sorted(enum - emitted))} has no row with the event",
        )
    for row in model.reasons:
        if row.on_intake == "not-expected" and row.recovery == "replay":
            raise Refused("unexpected-recovery", f"{row.code} is not expected and is replayed")
        if row.on_intake == "expected" and row.recovery is None:
            raise Refused("unexpected-recovery", f"{row.code} is expected and has no recovery")


def check_tenancy(model: Tenancy) -> None:
    patterns = profile_patterns()
    for name, profile in model.profiles.items():
        for tenant in profile.tenants:
            if not patterns["tenant"].fullmatch(tenant):
                raise Refused("bad-tenant", f"{name}: {tenant!r} breaks the tenant pattern")
    for name, profile in model.profiles.items():
        for cls in profile.priority_classes:
            if not patterns["priorityclass"].fullmatch(cls):
                raise Refused("bad-class", f"{name}: {cls!r} breaks the priorityclass pattern")
        for cls in profile.caps.job_class:
            if not patterns["jobclass"].fullmatch(cls):
                raise Refused("bad-class", f"{name}: {cls!r} breaks the jobclass pattern")
    for name, profile in model.profiles.items():
        caps = profile.caps
        top = caps.global_.value
        if top > caps.open.value:
            raise Refused("cap-above-global", f"{name}: caps.global is above caps.open")
        for cls, cap in caps.job_class.items():
            if cap.value > top:
                raise Refused("cap-above-global", f"{name}: the {cls} cap is above caps.global")
        for tenant, entry in profile.tenants.items():
            if entry.quota.value > top:
                raise Refused(
                    "cap-above-global", f"{name}: the quota of {tenant} is above caps.global"
                )
    for name in model.profiles:
        if name not in PRESETS:
            raise Refused("unknown-profile", f"{name} is not a preset: {', '.join(PRESETS)}")
    for tilt, name in model.tilt_profiles.items():
        if name not in model.profiles:
            raise Refused("unknown-profile", f"the Tilt profile {tilt} maps to no profile {name}")


CHECKS = {"topology": check_topology, "reasons": check_reasons, "tenancy": check_tenancy}


def parse(name: str, text: str) -> BaseModel:
    """The model of `name` validated from YAML text, with every check of its contract."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise Refused("schema-violation", " ".join(str(error).split())) from error
    try:
        model = FILES[name][0].model_validate(document)
    except ValidationError as error:
        raise Refused("schema-violation", " ".join(str(error).split())) from error
    CHECKS[name](model)
    return model


@cache
def _pinned(name: str) -> BaseModel:
    return parse(name, (CONTRACT / f"{name}.yaml").read_text())


def topology() -> Topology:
    """The pinned topology, read through package data."""
    return _pinned("topology")


def reasons() -> Reasons:
    """The pinned reason table, read through package data."""
    return _pinned("reasons")


def tenancy() -> Tenancy:
    """The pinned tenancy file, read through package data."""
    return _pinned("tenancy")


def name_of(path: Path) -> str | None:
    parts = path.parts
    if "vectors" in parts and parts.index("vectors") + 1 < len(parts) - 1:
        name = parts[parts.index("vectors") + 1]
    else:
        name = path.name.split(".")[0]
    return name if name in FILES else None


def check(path: Path, name: str) -> None:
    """Raises Refused with the first rule that the file of the schema `name` breaks."""
    try:
        text = path.read_bytes().decode()
    except UnicodeDecodeError as error:
        raise Refused("schema-violation", " ".join(str(error).split())) from error
    parse(name, text)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dsy_broker.contract", exit_on_error=False)
    parser.add_argument("--check", type=Path, required=True)
    parser.add_argument("--as", dest="name", choices=sorted(FILES))
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return 2
    name = args.name or name_of(args.check)
    if not args.check.is_file() or name is None:
        print(
            "usage: contract --check FILE [--as NAME], NAME one of " + ", ".join(FILES),
            file=sys.stderr,
        )
        return 2
    try:
        check(args.check, name)
    except Refused as refused:
        print(f"reason: {refused}", file=sys.stderr)
        return 1
    print(f"ok {args.check}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
