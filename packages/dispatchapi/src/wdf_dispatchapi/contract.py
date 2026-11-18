"""Loads one file of the API contract and checks it against the models, the reason table, the
status contract and the stage contract, read through package data.

Usage: python -m wdf_dispatchapi.contract --check FILE [--as NAME]
NAME is a key of models.FILES. Without it, the name is the directory under vectors/ that holds
the file, or else the file's stem, so routes.yaml is checked as routes.
Exit 0 valid, 1 invalid with "reason: <code>: <detail>" on stderr, 2 usage.
"""

import argparse
import hashlib
import json
import sys
from functools import cache
from pathlib import Path
from typing import get_args

import yaml
from pydantic import BaseModel, ValidationError

from .models import (
    CONTRACT,
    ENTRY_LINE_MAX_BYTES,
    FILES,
    HEADER_LINE_MAX_BYTES,
    Action,
    ControlTable,
    EntryAnswer,
    EntryError,
    ErrorBody,
    ErrorLine,
    Fingerprints,
    ReasonRow,
    Reasons,
    Routes,
)

YAML_FILES = ("routes", "reasons", "control")
LINE_MAX_BYTES = {"batch-header": HEADER_LINE_MAX_BYTES, "batch-entry": ENTRY_LINE_MAX_BYTES}
ACTIONS = get_args(Action)
# Whether a job in a status can have a workflow: a row at accepted has none, a row at admitted or
# running is treated as having one, and for every other status the gateway is asked.
WORKFLOW_CASES = {"accepted": ("without",), "admitted": ("with",), "running": ("with",)}
EITHER = ("with", "without")
PATCH_CASES = ("none", "same", "other")


class Refused(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def canonical(fields: dict) -> str:
    """The canonical JSON of the fingerprint fields: sorted keys, no spaces, no ASCII escaping."""
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(
    *, job_class: str, manifest_sha256: str, parameters: dict, priority_class: str, seed: int
) -> str:
    """The lower-case hex SHA-256 of the canonical JSON of the fields that define a job."""
    fields = {
        "job_class": job_class,
        "manifest_sha256": manifest_sha256,
        "parameters": parameters,
        "priority_class": priority_class,
        "seed": seed,
    }
    return hashlib.sha256(canonical(fields).encode()).hexdigest()


@cache
def _status() -> dict:
    return yaml.safe_load((CONTRACT / "status.yaml").read_text())


@cache
def control_events() -> frozenset[str]:
    """The event types that the stage contract gives to the control stage."""
    stages = yaml.safe_load((CONTRACT / "stages.yaml").read_text())["stages"]
    return frozenset(e["type"] for e in stages["control"]["emits"])


def _reason_rows() -> dict[str, ReasonRow]:
    return {row.code: row for row in reasons().reasons}


def _check_codes(codes: list[tuple[str, str]], scopes: set[str] | None) -> None:
    """Each (code, place) names a code of reasons.yaml whose scopes meet `scopes`, or, when
    `scopes` is None, a code that is ever answered with an HTTP status of its own."""
    rows = _reason_rows()
    for code, place in codes:
        if code not in rows:
            raise Refused("unknown-reason", f"{place}: {code} is not a code of reasons.yaml")
    for code, place in codes:
        row = rows[code]
        if scopes is None and row.http is None:
            raise Refused("reason-scope", f"{place}: {code} is carried only in a line")
        if scopes is not None and not scopes & set(row.scopes):
            raise Refused(
                "reason-scope",
                f"{place}: {code} has the scopes {', '.join(row.scopes)}, "
                f"and this place takes {', '.join(sorted(scopes))}",
            )


def check_control(model: ControlTable) -> None:
    codes = [(c.refusal, f"cell {i}") for i, c in enumerate(model.cells) if c.refusal]
    codes += [(r.refusal, f"overlay rule {i}") for i, r in enumerate(model.overlay) if r.refusal]
    _check_codes(codes, {"control"})

    allowed = {(t["from"], t["to"]): t.get("row_only", False) for t in _status()["transitions"]}
    for i, cell in enumerate(model.cells):
        for status in cell.status:
            current = status
            for move in cell.moves:
                row_only = allowed.get((current, move.to))
                if row_only is None or row_only != move.row_only:
                    kind = "row-only move" if move.row_only else "move"
                    raise Refused(
                        "transition-not-allowed",
                        f"cell {i}: a {kind} {current} -> {move.to} is not in status.yaml",
                    )
                if move.row_only and cell.workflow != "without":
                    raise Refused(
                        "transition-not-allowed",
                        f"cell {i}: a row-only move {current} -> {move.to} for a job "
                        f"{cell.workflow} a workflow",
                    )
                current = move.to

    emitted = control_events()
    for i, cell in enumerate(model.cells):
        for event in cell.events:
            if event not in emitted:
                raise Refused(
                    "event-not-of-stage", f"cell {i}: the control stage never emits {event}"
                )

    for status in _status()["statuses"]:
        for workflow in WORKFLOW_CASES.get(status, EITHER):
            for action in ACTIONS:
                for patch in PATCH_CASES if action == "resume" else ("none",):
                    found = [
                        c
                        for c in model.cells
                        if status in c.status
                        and c.workflow in (workflow, "either")
                        and c.action == action
                        and patch in c.patch
                    ]
                    if len(found) != 1:
                        raise Refused(
                            "control-cell-missing",
                            f"{len(found)} cells for {action} of a job at {status} {workflow} "
                            f"a workflow with patch {patch}, where one is needed",
                        )
    for outstanding in ACTIONS:
        for action in ACTIONS:
            found = [
                r for r in model.overlay if r.outstanding == outstanding and action in r.action
            ]
            if len(found) != 1:
                raise Refused(
                    "control-cell-missing",
                    f"{len(found)} overlay rules for {action} while {outstanding} is outstanding, "
                    "where one is needed",
                )


def check_fingerprint(model: Fingerprints) -> None:
    for vector in model.vectors:
        fields = vector.input.model_dump()
        text = canonical(fields)
        if text != vector.canonical:
            raise Refused("fingerprint-mismatch", f"{vector.name}: the canonical JSON is {text}")
        output = fingerprint(**fields)
        if output != vector.output:
            raise Refused("fingerprint-mismatch", f"{vector.name}: the fingerprint is {output}")


def check_entry_answer(model: EntryAnswer) -> None:
    if model.reason is not None:
        _check_codes([(model.reason, "reason")], {"entry"})


def check_entry_error(model: EntryError) -> None:
    _check_codes([(model.code, "code")], {"control", "any"})


def check_error_line(model: ErrorLine) -> None:
    _check_codes([(model.error.code, "error.code")], {"call", "any"})


def check_error(model: ErrorBody) -> None:
    _check_codes([(model.code, "code")], None)


CHECKS = {
    "control": check_control,
    "fingerprint": check_fingerprint,
    "entry-answer": check_entry_answer,
    "entry-error": check_entry_error,
    "error-line": check_error_line,
    "error": check_error,
}


def parse(name: str, text: str) -> BaseModel:
    """The model of `name` validated from its text, with every check of the contract."""
    model_type = FILES[name]
    try:
        if name in YAML_FILES:
            model = model_type.model_validate(yaml.safe_load(text))
        else:
            model = model_type.model_validate_json(text)
    except (yaml.YAMLError, ValidationError) as error:
        raise Refused("schema-violation", " ".join(str(error).split())) from error
    limit = LINE_MAX_BYTES.get(name)
    if limit is not None and len(text.rstrip("\n").encode()) > limit:
        raise Refused("too-large", f"a {name} line is above {limit} bytes")
    if name in CHECKS:
        CHECKS[name](model)
    return model


@cache
def _pinned(name: str) -> BaseModel:
    return parse(name, (CONTRACT / f"{name}.yaml").read_text())


def routes() -> Routes:
    """The pinned route table, read through package data."""
    return _pinned("routes")


def reasons() -> Reasons:
    """The pinned reason table, read through package data."""
    return _pinned("reasons")


def control_table() -> ControlTable:
    """The pinned control table, read through package data."""
    return _pinned("control")


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
    parser = argparse.ArgumentParser(prog="wdf_dispatchapi.contract", exit_on_error=False)
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
