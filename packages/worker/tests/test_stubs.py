"""A stub raises the lane error for exactly the names of its module's STUBBED, and nothing else,
and every stubbed callable raises it when called."""

import asyncio
import importlib
import inspect

import pytest

LANES = {
    "runner": "R",
    "pools.compile": "T",
    "pools.simulate": "T",
    "pools.aggregate": "T",
    "activity": "A",
    "service": "A",
    "__main__": "A",
    "probe": "A",
}
MODULES = {name: importlib.import_module(f"wdf_worker.{name}") for name in LANES}


def defined(module) -> dict:
    return {
        name: obj
        for name, obj in inspect.getmembers(module)
        if not name.startswith("_")
        and getattr(obj, "__module__", None) == module.__name__
        and (inspect.isfunction(obj) or inspect.isclass(obj))
    }


def callables(obj) -> list:
    """The function, or each method and property getter a class defines itself."""
    if not inspect.isclass(obj):
        return [obj]
    found = []
    for member in vars(obj).values():
        member = member.fget if isinstance(member, property) else member
        member = member.__func__ if isinstance(member, classmethod) else member
        if inspect.isfunction(member):
            found.append(member)
    return found


def call(function) -> None:
    """Calls with None for every parameter, awaiting a coroutine function."""
    result = function(*[None] * len(inspect.signature(function).parameters))
    if inspect.iscoroutine(result):
        asyncio.run(result)
    elif inspect.isasyncgen(result):
        asyncio.run(anext(result))


@pytest.mark.parametrize("module", MODULES)
def test_every_stub_of_a_module_is_listed(module):
    """A public name defined in a module that raises the lane error must be in STUBBED."""
    stubbed = MODULES[module].STUBBED
    for name, obj in defined(MODULES[module]).items():
        raises = "not implemented: owned by lane" in inspect.getsource(obj)
        assert raises == (name in stubbed), name


@pytest.mark.parametrize("module", MODULES)
def test_every_listed_stub_raises_its_lane_error(module):
    found = defined(MODULES[module])
    for name in MODULES[module].STUBBED:
        for function in callables(found[name]):
            with pytest.raises(NotImplementedError) as raised:
                call(function)
            assert str(raised.value) == f"not implemented: owned by lane {LANES[module]}", name
