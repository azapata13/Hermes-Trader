"""Clock abstraction.

The deterministic engine never calls these directly: time enters the engine only as event
fields. Clocks are used at the boundary (adapter callback entry, recorder, watchdog) and by
replay/tests (``ManualClock``).

* ``mono_ns``: monotonic, high resolution (``time.perf_counter_ns``) — latency and ordering.
* ``wall_ns``: wall clock (``time.time_ns``) — alignment with exchange timestamps and bars.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def mono_ns(self) -> int: ...

    def wall_ns(self) -> int: ...


class SystemClock:
    """Live clock."""

    __slots__ = ()

    def mono_ns(self) -> int:
        return time.perf_counter_ns()

    def wall_ns(self) -> int:
        return time.time_ns()


class ManualClock:
    """Deterministic clock for tests and replay. Time only moves when told to, never backwards."""

    __slots__ = ("_mono", "_wall")

    def __init__(self, mono_ns: int = 0, wall_ns: int = 0) -> None:
        if mono_ns < 0 or wall_ns < 0:
            raise ValueError("clock values must be >= 0")
        self._mono = mono_ns
        self._wall = wall_ns

    def mono_ns(self) -> int:
        return self._mono

    def wall_ns(self) -> int:
        return self._wall

    def set(self, mono_ns: int, wall_ns: int) -> None:
        if mono_ns < self._mono or wall_ns < self._wall:
            raise ValueError("ManualClock cannot move backwards")
        self._mono = mono_ns
        self._wall = wall_ns

    def advance(self, ns: int) -> None:
        if ns < 0:
            raise ValueError("ManualClock cannot move backwards")
        self._mono += ns
        self._wall += ns
