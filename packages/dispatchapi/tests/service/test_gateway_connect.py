"""The gateway builds its client through the shared `dsy_temporal.client.connect`."""

from dsy_dispatchapi import gateway


def test_gateway_connects_through_the_shared_helper(monkeypatch):
    seen = {}

    async def fake_connect(address, namespace, **kwargs):
        seen.update(address=address, namespace=namespace, kwargs=kwargs)
        return object()

    monkeypatch.setattr(gateway, "connect", fake_connect)
    with gateway.TemporalGateway("temporal:7233"):
        pass

    assert seen["address"] == "temporal:7233"
    assert seen["namespace"] == gateway.naming().namespace
    assert seen["kwargs"] == {"lazy": True}
