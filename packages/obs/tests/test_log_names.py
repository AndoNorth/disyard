"""No log call under `packages/*/src` names a log field of instrumentation version 3: no `extra=`
key and no `logs.bind` keyword is `jobid`, `taskid`, `attempt` or `traceparent`, except the
`traceparent` argument of `logs.bind`, from which the helper derives `trace_id` and `span_id`."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
OLD = {"jobid", "taskid", "attempt", "traceparent"}
SOURCES = sorted(ROOT.glob("packages/*/src/**/*.py"))


def keys(node: ast.expr) -> set[str]:
    """The literal keys of a dict display or of a `dict(...)` call."""
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant)}
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "dict":
        return {k.arg for k in node.keywords if k.arg}
    return set()


def is_bind(func: ast.expr) -> bool:
    """`logs.bind`, also reached as `dsy_obs.logs.bind`."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "bind"
        and getattr(func.value, "id", getattr(func.value, "attr", "")) == "logs"
    )


def old_names(source: str) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "extra":
                found += [(node.lineno, k) for k in sorted(keys(kw.value) & OLD)]
        if is_bind(node.func):
            names = {kw.arg for kw in node.keywords if kw.arg} - {"traceparent"}
            found += [(node.lineno, k) for k in sorted(names & OLD)]
    return found


def test_no_log_call_names_a_version_3_field():
    assert len(SOURCES) > 50, "the glob found too few sources"
    found = [
        f"{path.relative_to(ROOT)}:{line}: {name}"
        for path in SOURCES
        for line, name in old_names(path.read_text())
    ]
    assert not found, found


@pytest.mark.parametrize(
    "source",
    [
        'log.info("x", extra={"jobid": job})',
        'log.info("x", extra=dict(taskid=t))',
        "logs.bind(attempt=1)",
        "dsy_obs.logs.bind(jobid=j)",
    ],
)
def test_a_planted_old_name_fails(source):
    assert old_names(source)


@pytest.mark.parametrize(
    "source",
    [
        "logs.bind(traceparent=tp)",
        'log.info("x", extra={"job_id": job, "trace_id": t})',
        'ConfigDict(extra="forbid")',
    ],
)
def test_the_new_names_and_the_bind_traceparent_pass(source):
    assert not old_names(source)
