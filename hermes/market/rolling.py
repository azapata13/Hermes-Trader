"""Exact rolling integer sums for the C7/C8 1 s / 5 s / 30 s windows (C9f performance).

A derived CACHE only: it never feeds the market state hash (``fingerprint_state`` is unchanged) and it
is proven behavior-identical to the original full rescans (tests + before/after replays).

``WindowSums`` keeps, for ONE event stream, the event times and the running (cumulative) integer sums
since the last ``clear()``, plus one head index per window. A window's sums are
``cum[last] - cum[head - 1]`` where ``head`` is the first entry with ``t >= now - window`` — exactly
the rescan's inclusion rule (``t >= cutoff``). Heads only move forward, so every entry is appended
once and passed once per window: O(1) per event, amortized O(1) per snapshot, bounded memory
(entries older than the longest window are compacted away).

Exactness guard: the rescan counts every retained entry with ``t >= cutoff`` regardless of order,
while head advancement assumes time order. If an entry arrives out of time order or older than an
already-applied cutoff, or ``now`` goes backwards, the cache marks itself inexact and callers fall back
to the original rescan until the next ``clear()``. Live and replay feed monotone ``recv_mono_ns``, so
the fallback is a safety net, not the normal path.
"""

from __future__ import annotations

from operator import add, sub

_NEG = -(1 << 62)
_COMPACT = 2048


class WindowSums:
    __slots__ = ("windows_ns", "width", "_t", "_c", "_off", "_pre", "_zero", "_heads", "_cuts", "_last_t",
                 "_maxcut", "_pending", "exact")

    def __init__(self, windows_ns: tuple[int, ...], width: int) -> None:
        self.windows_ns = windows_ns
        self.width = width
        self._zero = (0,) * width
        self.clear()

    def clear(self) -> None:
        self._t: list[int] = []
        self._c: list[tuple[int, ...]] = []
        self._off = 0                        # absolute index of _t[0]
        self._pre = self._zero               # cumulative sums just before absolute index _off
        self._heads = [0] * len(self.windows_ns)
        self._cuts = [_NEG] * len(self.windows_ns)
        self._last_t = _NEG
        self._maxcut = _NEG                  # the largest cutoff applied so far (shortest window)
        self._pending = 0
        self.exact = True

    def push(self, t: int, vals: tuple[int, ...], now_ns: int) -> None:
        """O(1): heads are advanced lazily (at ``sums_at``) and every ``_COMPACT`` pushes (bounded memory)."""
        if t < self._last_t or t < self._maxcut:
            self.exact = False
        self._last_t = t
        c = self._c
        self._t.append(t)
        c.append(tuple(map(add, c[-1] if c else self._pre, vals)))
        self._pending += 1
        if self._pending >= _COMPACT:
            self.advance(now_ns)

    def advance(self, now_ns: int) -> None:
        self._pending = 0
        ts, off = self._t, self._off
        end = off + len(ts)
        heads, cuts = self._heads, self._cuts
        for j, w in enumerate(self.windows_ns):
            cut = now_ns - w
            if cut <= cuts[j]:
                if cut < cuts[j]:
                    self.exact = False
                continue
            cuts[j] = cut
            if cut > self._maxcut:
                self._maxcut = cut
            h = heads[j]
            while h < end and ts[h - off] < cut:
                h += 1
            heads[j] = h
        m = min(heads) - off
        if m > _COMPACT and m > len(ts) // 2:
            self._pre = self._c[m - 1]
            del ts[:m]
            del self._c[:m]
            self._off += m

    def sums_at(self, j: int, now_ns: int) -> tuple[int, ...] | None:
        """Sums of window ``j`` at ``now_ns`` or None when the cache is inexact (use the rescan)."""
        if not self.exact:
            return None
        self.advance(now_ns)
        if not self.exact:
            return None
        h, off, c = self._heads[j], self._off, self._c
        last = c[-1] if c else self._pre
        before = self._pre if h == off else c[h - 1 - off]
        return tuple(map(sub, last, before))

    def __len__(self) -> int:
        return len(self._t)
