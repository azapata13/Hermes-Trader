"""Deterministic replay clock (C6).

Implements the ``hermes.core.clock.Clock`` protocol from RECORDED event times only: ``mono_ns`` /
``wall_ns`` return the ``recv_mono_ns`` / ``recv_wall_ns`` of the last raw event observed (never
moving backwards). Recorded timer ticks carry their own ``due_mono_ns`` / ``coalesced``. Nothing
here reads a system clock, so replay behavior cannot depend on when or how fast it runs.
"""

from __future__ import annotations


class ReplayClock:
    __slots__ = ("_mono", "_wall", "events", "first_mono_ns", "first_wall_ns")

    def __init__(self) -> None:
        self._mono = 0
        self._wall = 0
        self.events = 0
        self.first_mono_ns: int | None = None
        self.first_wall_ns: int | None = None

    def observe(self, raw) -> None:
        m, w = raw.recv_mono_ns, raw.recv_wall_ns
        if self.first_mono_ns is None:
            self.first_mono_ns, self.first_wall_ns = m, w
        if m > self._mono:
            self._mono = m
        if w > self._wall:
            self._wall = w
        self.events += 1

    def mono_ns(self) -> int:
        return self._mono

    def wall_ns(self) -> int:
        return self._wall

    @property
    def recorded_span_ns(self) -> int:
        return 0 if self.first_mono_ns is None else self._mono - self.first_mono_ns
