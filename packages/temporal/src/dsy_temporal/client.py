"""The Temporal client recipe: the event data converter and the tracing interceptor."""

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.runtime import Runtime

from .converter import event_data_converter
from .trace import tracing_interceptor


async def connect(
    address: str,
    namespace: str,
    *,
    runtime: Runtime | None = None,
    lazy: bool = False,
    check_namespace: bool = False,
) -> Client:
    """A client with the event data converter and the tracing interceptor.

    `lazy` returns without reaching the server, so a service can start while Temporal is down.
    `check_namespace` fails at once when the server does not know the namespace."""
    kwargs = {"runtime": runtime} if runtime is not None else {}
    client = await Client.connect(
        address,
        namespace=namespace,
        data_converter=event_data_converter,
        interceptors=[tracing_interceptor()],
        lazy=lazy,
        **kwargs,
    )
    if check_namespace:
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=namespace)
        )
    return client
