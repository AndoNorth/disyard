"""Commands run inside a pod, for the probes that no API shows."""

from dsy_clusterkit import kubectl


def exec_in(
    context: str, namespace: str, pod: str, *argv: str, container: str | None = None
) -> str:
    """The standard output of `argv` run in the pod, or CalledProcessError on a failure."""
    chosen = ["-c", container] if container else []
    return kubectl(context, f"--namespace={namespace}", "exec", pod, *chosen, "--", *argv)


def process_names(context: str, namespace: str, pod: str, container: str | None = None) -> set[str]:
    """The names of the processes in the pod, read from /proc so the image needs no `ps`."""
    out = exec_in(context, namespace, pod, "sh", "-c", "cat /proc/[0-9]*/comm", container=container)
    return {line.strip() for line in out.splitlines() if line.strip()}
