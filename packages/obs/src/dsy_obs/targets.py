"""The scrape jobs of the stack's Prometheus, the set of `job` labels a healthy scrape shows.

The rendered Prometheus configuration is held equal to `JOBS`, and the telemetry row of the smoke
gate reads it.
"""

JOBS = frozenset(
    {
        # Every pod with a container port named `metrics` or `prometheus`, or the scrape annotation.
        "kubernetes-pods",
        # RabbitMQ's detailed families, the job the instrumentation catalogue names for them.
        "rabbitmq-detailed",
        # Each node's kubelet on port 10250, and its cAdvisor path.
        "kubelet",
        "cadvisor",
    }
)
