from __future__ import annotations

import pytest

from hermes.core.clock import Clock, ManualClock, SystemClock


def test_system_clock_monotonic_and_protocol():
    c = SystemClock()
    assert isinstance(c, Clock)
    a, b = c.mono_ns(), c.mono_ns()
    assert b >= a
    assert c.wall_ns() > 1_600_000_000 * 10**9


def test_manual_clock():
    c = ManualClock(10, 1_000)
    assert isinstance(c, Clock)
    c.advance(5)
    assert (c.mono_ns(), c.wall_ns()) == (15, 1_005)
    c.set(20, 2_000)
    assert (c.mono_ns(), c.wall_ns()) == (20, 2_000)


def test_manual_clock_never_goes_backwards():
    c = ManualClock(10, 10)
    with pytest.raises(ValueError):
        c.advance(-1)
    with pytest.raises(ValueError):
        c.set(5, 20)
    with pytest.raises(ValueError):
        ManualClock(-1, 0)
