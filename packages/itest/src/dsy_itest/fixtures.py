"""The `t1` fixture, the markers `pair` and `isolated` and the isolated deselection, registered as a
pytest plugin by the package entry point."""

import re

import pytest

from .session import REFUSED_EXIT, LockTimeout, Session

ISOLATED = "isolated"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "pair: needs a store and a database in one session, which holds two containers"
    )
    config.addinivalue_line(
        "markers",
        f"{ISOLATED}: starts containers of its own, runs only under `-m {ISOLATED}` and holds at "
        "most two containers",
    )


def names_isolated(expression: str) -> bool:
    """Whether a marker expression names `isolated` anywhere but directly after `not`."""
    # ponytail: a token scan, so `not (isolated or x)` counts as naming it, parse if that matters
    tokens = re.findall(r"[\w.\-]+|\S", expression)
    return any(
        token == ISOLATED and (i == 0 or tokens[i - 1] != "not") for i, token in enumerate(tokens)
    )


def pytest_collection_modifyitems(config, items):
    """Deselects every isolated test unless the marker expression asks for them."""
    if names_isolated(config.option.markexpr or ""):
        return
    kept, deselected = [], []
    for item in items:
        (deselected if item.get_closest_marker(ISOLATED) else kept).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


@pytest.fixture(scope="session")
def t1(request):
    """A T1 session. A missing Docker fails the test and is never a skip.

    It holds two containers when any collected test carries the marker `pair`, and one otherwise.
    A lock wait that runs out ends the run with exit 4 and a `refused:` message, not a test failure.
    """
    paired = any(item.get_closest_marker("pair") for item in request.session.items)
    session = Session(containers=2 if paired else 1)
    try:
        session.__enter__()
    except LockTimeout as exc:
        pytest.exit(str(exc), returncode=REFUSED_EXIT)
    try:
        yield session
    finally:
        session.close()
