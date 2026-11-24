"""The one call each service makes at startup: logs, traces and profiles, once per process.

from dsy_obs.setup import setup
setup("simulate-worker", pool="simulate")
"""

import threading

from dsy_obs import logs, profiles, traces

_installed: str | None = None
_lock = threading.Lock()


def setup(component: str, pool: str | None = None) -> None:
    """Installs the JSON log lines and, by their environment variables, the log and span push and
    the profiler, named after `component`; a later call with the same component does nothing."""
    global _installed
    with _lock:
        if _installed is not None:
            if _installed != component:
                raise RuntimeError(f"obs is set up for {_installed!r}, not {component!r}")
            return
        logs.install(component)
        traces.install(component)
        profiles.install(component, pool)
        _installed = component
