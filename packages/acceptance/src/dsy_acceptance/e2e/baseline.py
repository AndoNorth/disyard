"""Counters asserted as a change from a quiet baseline.

A job counter has no tenant label, so only the cluster lock makes the baseline quiet: nothing else
moves the counter between the read before a scenario and the read after it.
"""

from collections.abc import Callable

from . import wait


class Baseline:
    """Reads one counter through `read`, which returns its current value (0 while the series does
    not exist yet), and tells how far it moved since `take`."""

    def __init__(self, name: str, read: Callable[[], float]):
        self.name, self.read = name, read
        self.before: float | None = None

    def take(self) -> float:
        self.before = self.read()
        return self.before

    def delta(self) -> float:
        if self.before is None:
            raise RuntimeError(f"the baseline of {self.name} was not taken")
        return self.read() - self.before

    def rose_by(self, amount: float, timeout_s: float) -> float:
        """The change once it reaches `amount`, polled until the timeout. A change beyond `amount`
        fails at once, because the baseline was not quiet."""

        def reached() -> float | None:
            moved = self.delta()
            if moved > amount:
                raise AssertionError(f"{self.name} rose by {moved:g}, above the {amount:g} asked")
            return moved if moved == amount else None

        try:
            return wait(reached, timeout_s, f"{self.name} to rise by {amount:g}")
        except TimeoutError:
            raise AssertionError(
                f"{self.name} rose by {self.delta():g} in {timeout_s:.0f} s, not {amount:g}"
            ) from None
