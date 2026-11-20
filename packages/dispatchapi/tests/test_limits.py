"""The limits of a submit call: the contract's defaults, set from the environment, and the submit
calls in flight per tenant and per process."""

import pytest
from wft_dispatchapi.limits import CallSlots, ConfigError, Limits, TooManyCalls


def test_defaults_are_the_contract_limits():
    assert Limits() == Limits(
        entries=100_000,
        entry_line_bytes=2_048,
        header_line_bytes=1_052_672,
        calls_per_tenant=2,
        calls_per_process=4,
        requests=16,
        idle_timeout_s=60.0,
        write_timeout_s=60.0,
        chunk_entries=1_000,
    )


def test_limits_from_the_environment():
    environ = {"WFT_DISPATCH_ENTRIES": "10", "WFT_DISPATCH_IDLE_TIMEOUT_S": "0.5", "OTHER": "1"}

    assert Limits.from_env(environ) == Limits(entries=10, idle_timeout_s=0.5)
    assert Limits.from_env({}) == Limits()


@pytest.mark.parametrize("value", ["ten", "1.5", "0", "-3"])
def test_limits_refuse_a_bad_value(value):
    with pytest.raises(ConfigError, match="ENTRIES|entries"):
        Limits.from_env({"WFT_DISPATCH_ENTRIES": value})


def test_calls_per_tenant():
    slots = CallSlots(Limits())
    with slots.hold("acme"), slots.hold("acme"):
        with pytest.raises(TooManyCalls, match="tenant acme"), slots.hold("acme"):
            pass
        with slots.hold("other"):
            pass


def test_calls_per_process():
    slots = CallSlots(Limits())
    with slots.hold("a"), slots.hold("b"), slots.hold("c"), slots.hold("d"):
        with pytest.raises(TooManyCalls, match="process"), slots.hold("e"):
            pass


def test_a_call_frees_its_slot_when_it_fails():
    slots = CallSlots(Limits(calls_per_tenant=1))
    with pytest.raises(RuntimeError), slots.hold("acme"):
        raise RuntimeError("the call failed")
    with slots.hold("acme"):
        pass
