"""Allocation-free latency histograms (log2 nanosecond buckets).

``record`` is a handful of integer operations and is safe to call on the dispatch thread.
``swap`` (reporter thread) atomically replaces the bucket list and returns a summary of the
previous window; an observation racing with a swap may land in either window (acceptable
for telemetry). Percentiles are bucket upper bounds (<= 2x resolution), max is exact.
"""

from __future__ import annotations

from dataclasses import dataclass

NBUCKETS = 48   # 2^47 ns ~ 39 h


@dataclass(frozen=True, slots=True)
class LatencySummary:
    count: int
    mean_ns: float
    p50_ns: int
    p90_ns: int
    p99_ns: int
    max_ns: int

    def as_us(self) -> dict[str, float]:
        return {"n": self.count, "mean": round(self.mean_ns / 1000, 1), "p50": round(self.p50_ns / 1000, 1),
                "p90": round(self.p90_ns / 1000, 1), "p99": round(self.p99_ns / 1000, 1),
                "max": round(self.max_ns / 1000, 1)}


class LatencyHistogram:
    __slots__ = ("_b", "_n", "_sum", "_max")

    def __init__(self) -> None:
        self._b = [0] * NBUCKETS
        self._n = 0
        self._sum = 0
        self._max = 0

    def record(self, ns: int) -> None:
        if ns < 0:
            ns = 0
        i = ns.bit_length()
        if i >= NBUCKETS:
            i = NBUCKETS - 1
        self._b[i] += 1
        self._n += 1
        self._sum += ns
        if ns > self._max:
            self._max = ns

    @property
    def count(self) -> int:
        return self._n

    def summary(self) -> LatencySummary:
        return _summarize(self._b, self._n, self._sum, self._max)

    def swap(self) -> LatencySummary:
        b, n, s, m = self._b, self._n, self._sum, self._max
        self._b = [0] * NBUCKETS
        self._n = 0
        self._sum = 0
        self._max = 0
        return _summarize(b, n, s, m)


def _pct(b: list[int], n: int, q: float) -> int:
    if n == 0:
        return 0
    target = q * n
    acc = 0
    for i, c in enumerate(b):
        acc += c
        if acc >= target:
            return (1 << i) if i > 0 else 0
    return 1 << (len(b) - 1)


def _summarize(b: list[int], n: int, s: int, m: int) -> LatencySummary:
    return LatencySummary(n, (s / n) if n else 0.0, min(_pct(b, n, 0.50), m), min(_pct(b, n, 0.90), m),
                          min(_pct(b, n, 0.99), m), m)
