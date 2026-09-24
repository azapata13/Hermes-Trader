"""PriceGrid: exact mapping between exchange prices and internal integer units.

Built from ``ContractDetails.minTick`` AND the contract's market rule (``reqMarketRule``),
which may define price bands with different increments (decision 7).

* ``unit``  — the internal integer unit (Decimal). It is the GCD of every band increment and
  ``min_tick``, so every legal price is an integer number of units.
  For MNQ (single band, increment 0.25): unit == increment == 0.25, i.e. units == ticks.
* Legality rule: a price is legal when it is a multiple of the increment of the band it falls
  in (bands are selected by ``low_edge``; prices below the first low edge use the first band).
* All arithmetic that defines the grid is exact (``Decimal``); the hot-path conversion
  ``to_units`` uses a precomputed float scale plus a tolerance check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

OFF_GRID_TOLERANCE_UNITS = 1e-6
_MAX_ABS_UNITS = 10**15  # far beyond any real price; guards against sentinel values


class OffGridPriceError(ValueError):
    """Price is non-finite, absurdly large, or not an integer number of grid units."""


class PriceGridError(ValueError):
    """Invalid grid definition."""


def _to_decimal(value: Decimal | str | float | int) -> Decimal:
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise PriceGridError(f"non-finite grid value {value!r}")
        d = Decimal(repr(value))  # shortest round-trip repr: 0.25 -> Decimal('0.25')
    else:
        d = Decimal(value)
    if not d.is_finite():
        raise PriceGridError(f"non-finite grid value {value!r}")
    return d


def _decimal_gcd(values: Sequence[Decimal]) -> Decimal:
    exp = min(v.as_tuple().exponent for v in values)  # most negative exponent
    scale = Decimal(10) ** (-exp)
    ints = [int((v * scale).to_integral_exact()) for v in values]
    g = 0
    for i in ints:
        g = math.gcd(g, i)
    return Decimal(g) / scale


@dataclass(frozen=True, slots=True)
class PriceBand:
    low_edge_units: int
    step_units: int


class PriceGrid:
    __slots__ = ("_unit", "_unit_f", "_inv_unit_f", "_bands", "_min_tick", "_uniform_step")

    def __init__(self, min_tick: Decimal | str | float, increments: Iterable[tuple[Decimal | str | float, Decimal | str | float]] = ()) -> None:
        mt = _to_decimal(min_tick)
        if mt <= 0:
            raise PriceGridError("min_tick must be > 0")
        incs = [(_to_decimal(lo), _to_decimal(inc)) for lo, inc in increments]
        if not incs:
            incs = [(Decimal(0), mt)]
        for lo, inc in incs:
            if inc <= 0:
                raise PriceGridError(f"increment must be > 0 (got {inc})")
            if lo < 0:
                raise PriceGridError(f"low_edge must be >= 0 (got {lo})")
        lows = [lo for lo, _ in incs]
        if lows != sorted(lows) or len(set(lows)) != len(lows):
            raise PriceGridError("market rule low edges must be strictly increasing")

        unit = _decimal_gcd([mt] + [inc for _, inc in incs] + [lo for lo, _ in incs if lo != 0])
        bands = []
        for lo, inc in incs:
            lo_u = lo / unit
            step_u = inc / unit
            if lo_u != lo_u.to_integral_value() or step_u != step_u.to_integral_value():
                raise PriceGridError("band not representable on unit grid")  # pragma: no cover (gcd guarantees)
            bands.append(PriceBand(int(lo_u), int(step_u)))

        self._unit = unit
        self._unit_f = float(unit)
        self._inv_unit_f = 1.0 / float(unit)
        self._bands = tuple(bands)
        self._min_tick = mt
        self._uniform_step = bands[0].step_units if len(bands) == 1 else None

    # ------------------------------------------------------------------ constructors
    @classmethod
    def uniform(cls, tick: Decimal | str | float) -> "PriceGrid":
        return cls(tick)

    @classmethod
    def from_market_rule(cls, min_tick: Decimal | str | float, increments: Iterable[tuple[Decimal | str | float, Decimal | str | float]]) -> "PriceGrid":
        incs = list(increments)
        if not incs:
            raise PriceGridError("market rule has no price increments")
        return cls(min_tick, incs)

    # ------------------------------------------------------------------ properties
    @property
    def unit(self) -> Decimal:
        return self._unit

    @property
    def min_tick(self) -> Decimal:
        return self._min_tick

    @property
    def bands(self) -> tuple[PriceBand, ...]:
        return self._bands

    @property
    def is_uniform(self) -> bool:
        return self._uniform_step is not None

    @property
    def min_tick_matches_rule(self) -> bool:
        """True when minTick equals the smallest band increment (IBKR data consistency check)."""
        smallest = min(b.step_units for b in self._bands) * self._unit
        return smallest == self._min_tick

    # ------------------------------------------------------------------ conversion
    def to_units(self, price: float) -> int:
        """Exchange price -> integer units. Raises OffGridPriceError on non-finite/off-grid prices."""
        if not math.isfinite(price):
            raise OffGridPriceError(f"non-finite price {price!r}")
        q = price * self._inv_unit_f
        if not (-_MAX_ABS_UNITS <= q <= _MAX_ABS_UNITS):
            raise OffGridPriceError(f"price {price!r} out of range")
        n = round(q)
        if abs(q - n) > OFF_GRID_TOLERANCE_UNITS:
            raise OffGridPriceError(f"price {price!r} is not a multiple of unit {self._unit}")
        return int(n)

    def to_units_or_none(self, price: float) -> int | None:
        try:
            return self.to_units(price)
        except OffGridPriceError:
            return None

    def to_price(self, units: int) -> float:
        return units * self._unit_f

    def to_decimal(self, units: int) -> Decimal:
        return units * self._unit

    # ------------------------------------------------------------------ band rules
    def _band_index(self, units: int) -> int:
        idx = 0
        for i, b in enumerate(self._bands):
            if units >= b.low_edge_units:
                idx = i
            else:
                break
        return idx

    def step_at(self, units: int) -> int:
        """Increment (in units) of the band containing ``units``."""
        if self._uniform_step is not None:
            return self._uniform_step
        return self._bands[self._band_index(units)].step_units

    def is_legal(self, units: int) -> bool:
        return units % self.step_at(units) == 0

    def next_up(self, units: int) -> int:
        """Smallest legal price strictly above ``units``."""
        if self._uniform_step is not None:
            s = self._uniform_step
            return (units // s + 1) * s
        # Multi-band: walk unit by unit (bounded by the largest step; not a hot path).
        c = units + 1
        while not self.is_legal(c):
            c += 1
        return c

    def next_down(self, units: int) -> int:
        """Largest legal price strictly below ``units``."""
        if self._uniform_step is not None:
            s = self._uniform_step
            return units - s if units % s == 0 else (units // s) * s
        c = units - 1
        while not self.is_legal(c):
            c -= 1
        return c

    def ticks_between(self, a: int, b: int) -> int:
        """Signed number of legal price steps from ``a`` to ``b`` (both must be legal)."""
        if not (self.is_legal(a) and self.is_legal(b)):
            raise OffGridPriceError("ticks_between requires legal prices")
        if self._uniform_step is not None:
            return (b - a) // self._uniform_step
        sign = 1
        if b < a:
            a, b, sign = b, a, -1
        count = 0
        c = a
        while c < b:
            c = self.next_up(c)
            count += 1
        return sign * count

    def __repr__(self) -> str:
        return f"PriceGrid(unit={self._unit}, min_tick={self._min_tick}, bands={self._bands})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PriceGrid):
            return NotImplemented
        return (self._unit, self._bands, self._min_tick) == (other._unit, other._bands, other._min_tick)

    def __hash__(self) -> int:
        return hash((self._unit, self._bands, self._min_tick))
