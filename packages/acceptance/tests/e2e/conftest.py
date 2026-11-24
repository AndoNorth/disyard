"""The harness of the level 4 scenarios: each test carries `@pytest.mark.scenario("<id>")`, runs in
the order of `dsy_acceptance.e2e.ORDER`, and the run ends with one summary line per scenario in the
row format of the smoke gate. Fixtures hand out unique batch keys, the API of the e2e tenant,
forwards to the observability stack and counter baselines taken from a quiet cluster."""

import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dsy_acceptance.clients import api, observability
from dsy_acceptance.e2e import CONTROL_API, DISPATCH, ORDER, TENANT, batch_key
from dsy_acceptance.e2e.baseline import Baseline
from dsy_acceptance.e2e.summary import Summary
from dsy_acceptance.smoke import forwarded

CALL_S = 30.0
# Short enough that a cleanup after the budget ran out ends before the timer's kill, 10 s later.
CANCEL_S = 5.0
OBS_NAMESPACE = "dsy-obs"

SUMMARY = pytest.StashKey[Summary]()
STARTED = pytest.StashKey[float]()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "scenario(id): the level 4 scenario a test belongs to")
    config.stash[SUMMARY] = Summary()
    config.stash[STARTED] = time.monotonic()


HERE = Path(__file__).parent


def is_scenario(item: pytest.Item) -> bool:
    """A conftest hook sees every test of the run, so each hook leaves the other levels alone."""
    return HERE in item.path.parents


def scenario_of(item: pytest.Item) -> str:
    marker = item.get_closest_marker("scenario")
    if marker is None or not marker.args:
        raise pytest.UsageError(f"{item.nodeid} has no @pytest.mark.scenario(<id>)")
    return marker.args[0]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marks every test e2e, so the unit run leaves them out, and puts them in the run order."""
    ours = [item for item in items if is_scenario(item)]
    for item in ours:
        item.add_marker(pytest.mark.e2e)
        if scenario_of(item) not in ORDER:
            raise pytest.UsageError(f"{item.nodeid} names a scenario not in ORDER")
    ours.sort(key=lambda item: ORDER.index(scenario_of(item)))
    items[:] = [item for item in items if not is_scenario(item)] + ours


REPORTS = pytest.StashKey[dict]()


def reason(report: pytest.TestReport) -> str:
    """The first line of what a failed or skipped phase says."""
    if report.skipped and isinstance(report.longrepr, tuple):
        return str(report.longrepr[2]).removeprefix("Skipped: ")
    crash = getattr(report.longrepr, "reprcrash", None)
    text = crash.message if crash else str(report.longrepr or report.outcome)
    return text.splitlines()[0]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Keeps each phase's report on the item, and feeds it to the summary of its scenario."""
    report = yield
    if not is_scenario(item):
        return report
    item.stash.setdefault(REPORTS, {})[report.when] = report
    detail = reason(report) if not report.passed else ""
    item.config.stash[SUMMARY].add(
        scenario_of(item), report.when, report.outcome, report.duration, detail
    )
    return report


def pytest_terminal_summary(terminalreporter, config: pytest.Config) -> None:
    if not config.stash[SUMMARY].scenarios:
        return
    elapsed = time.monotonic() - config.stash[STARTED]
    preset = os.environ.get("DSY_PRESET", "ci")
    for line in config.stash[SUMMARY].lines(preset, elapsed):
        terminalreporter.write_line(line)


@pytest.fixture(scope="session")
def client() -> api.Api:
    """The API of the e2e tenant."""
    return api.Api(DISPATCH, CONTROL_API, TENANT, CALL_S)


@pytest.fixture
def batches(request: pytest.FixtureRequest, client: api.Api) -> Iterator[Callable[[], str]]:
    """Hands out a fresh batch key per call. A test that fails leaves its batches cancelled, so a
    late job does not hold the pools into the next scenario."""
    keys: list[str] = []

    def fresh() -> str:
        keys.append(batch_key())
        return keys[-1]

    yield fresh
    reports = request.node.stash.get(REPORTS, {})
    if all(r.passed for r in reports.values()):
        return
    short = api.Api(DISPATCH, CONTROL_API, TENANT, CANCEL_S)
    for key in keys:
        try:
            print(
                f"cleanup: cancelled batch {key}: {short.cancel_batch(key, 'e2e')}", file=sys.stderr
            )
        except Exception as error:
            print(f"cleanup: the cancel of batch {key} failed: {error}", file=sys.stderr)


@pytest.fixture(scope="session")
def prometheus() -> Iterator[str]:
    """The base address of Prometheus through a forward held for the session."""
    with forwarded(OBS_NAMESPACE, "svc/prometheus", 9090) as address:
        yield f"http://{address}"


@pytest.fixture
def baseline(prometheus: str) -> Callable[[str, str], Baseline]:
    """`baseline(name, promql)` reads the sum of the expression, 0 while it has no series, and has
    already taken its value, so the change the scenario causes is read against it."""

    def read(expr: str) -> Callable[[], float]:
        def value() -> float:
            return sum(float(item["value"][1]) for item in observability.prom(prometheus, expr))

        return value

    def take(name: str, expr: str) -> Baseline:
        taken = Baseline(name, read(expr))
        taken.take()
        return taken

    return take
