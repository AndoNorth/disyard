"""Every function of the package is implemented, so the list of stubbed names is empty."""

import inspect

import wft_eventstore
from wft_eventstore import STUBBED

FUNCTIONS = [
    getattr(wft_eventstore, name)
    for name in wft_eventstore.__all__
    if inspect.isfunction(getattr(wft_eventstore, name))
]


def test_no_name_is_stubbed():
    assert STUBBED == ()


def test_no_function_raises_the_lane_error():
    assert {f.__name__ for f in FUNCTIONS} == {"chunk_count", "events_for_job", "insert_events"}
    assert all("not implemented" not in inspect.getsource(f) for f in FUNCTIONS)
