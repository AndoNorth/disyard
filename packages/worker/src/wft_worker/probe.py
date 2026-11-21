"""`python -m wft_worker.probe`: one line per platform endpoint, `<name>: ok` or `<name>: refused`,
and exit 0 when each answer is what the pool's NetworkPolicy allows.

Every pool reaches the Temporal frontend and the object store, only the compile pool, the one pod
given WFT_MIRROR, reaches the package mirror, and no pool reaches the app database. The collector
and Pyroscope are reached exactly when the pod has the telemetry endpoint, which the pool
NetworkPolicy opens to them only with the observability switch on.
"""

import os
import socket
import sys

from .config import MIRROR_ENV

STUBBED = ()

# The variable of the observability switch's container environment that the pod has only when on.
OBS_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

# The in-cluster Services, fixed by their modules, so the probe tests the policy and not its own
# configuration.
TARGETS = {
    "temporal": ("temporal-frontend.wft-infra.svc.cluster.local", 7233),
    "store": ("objectstore.wft-infra.svc.cluster.local", 8333),
    "mirror": ("mirror.wft-infra.svc.cluster.local", 80),
    "appdb": ("appdb.wft-infra.svc.cluster.local", 5432),
    "collector": ("collector.wft-obs.svc.cluster.local", 4318),
    "pyroscope": ("pyroscope.wft-obs.svc.cluster.local", 4040),
}
# A policy drops a packet rather than refuse it, so a connect that hangs this long is refused.
TIMEOUT_S = 3.0


def reaches(address: tuple[str, int]) -> bool:
    try:
        socket.create_connection(address, timeout=TIMEOUT_S).close()
    except OSError:
        return False
    return True


def allowed(environ) -> dict[str, bool]:
    mirror = bool(environ.get(MIRROR_ENV))
    obs = bool(environ.get(OBS_ENV))
    return {
        "temporal": True,
        "store": True,
        "mirror": mirror,
        "appdb": False,
        "collector": obs,
        "pyroscope": obs,
    }


def main(argv: list[str]) -> int:
    expected = allowed(os.environ)
    answers = {name: reaches(address) for name, address in TARGETS.items()}
    for name, ok in answers.items():
        print(f"{name}: {'ok' if ok else 'refused'}")
    return 0 if answers == expected else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
