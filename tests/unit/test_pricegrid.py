from __future__ import annotations

import math
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hermes.ibkr.market_rules import MarketRuleError, market_rule_id_for_exchange
from hermes.market.pricegrid import OffGridPriceError, PriceGrid, PriceGridError

MNQ = PriceGrid.from_market_rule(0.25, [(0.0, 0.25)])


def test_mnq_grid():
    assert MNQ.unit == Decimal("0.25")
    assert MNQ.is_uniform
    assert MNQ.min_tick_matches_rule
    assert MNQ.to_units(21234.75) == 84939
    assert MNQ.to_price(84939) == 21234.75
    assert MNQ.to_decimal(84939) == Decimal("21234.75")
    assert MNQ.ticks_between(84939, 84979) == 40          # 10 points = 40 ticks
    assert MNQ.next_up(84939) == 84940 and MNQ.next_down(84939) == 84938


def test_uniform_equals_rule_grid():
    assert PriceGrid.uniform("0.25") == MNQ
    assert hash(PriceGrid.uniform(0.25)) == hash(MNQ)


@pytest.mark.parametrize("price", [21234.1, 21234.3, 0.01, 0.25 + 1e-5])
def test_off_grid_rejected(price):
    with pytest.raises(OffGridPriceError):
        MNQ.to_units(price)
    assert MNQ.to_units_or_none(price) is None


@pytest.mark.parametrize("price", [math.nan, math.inf, -math.inf, 1.7976931348623157e308])
def test_non_finite_and_sentinels_rejected(price):
    with pytest.raises(OffGridPriceError):
        MNQ.to_units(price)


def test_float_noise_tolerated():
    assert MNQ.to_units(21234.75 + 1e-10) == 84939
    assert MNQ.to_units(0.1 + 0.2 - 0.05) == 1  # 0.25 with float noise


def test_multi_band_grid():
    # e.g. 0.01 below 1.00, 0.05 from 1.00
    g = PriceGrid.from_market_rule(0.01, [(0, 0.01), (1, 0.05)])
    assert g.unit == Decimal("0.01")
    assert not g.is_uniform
    assert g.step_at(50) == 1 and g.step_at(100) == 5 and g.step_at(250) == 5
    assert g.is_legal(99) and g.is_legal(105) and not g.is_legal(103)
    assert g.next_up(99) == 100 and g.next_up(100) == 105 and g.next_up(102) == 105
    assert g.next_down(105) == 100 and g.next_down(100) == 99
    assert g.ticks_between(98, 110) == 4     # 99, 100, 105, 110
    assert g.ticks_between(110, 98) == -4
    with pytest.raises(OffGridPriceError):
        g.ticks_between(98, 103)


def test_unit_is_gcd_of_increments():
    g = PriceGrid.from_market_rule(0.25, [(0, 0.25), (1000, 0.1)])
    assert g.unit == Decimal("0.05")
    assert g.to_units(1000.1) == 20002
    assert g.is_legal(g.to_units(1000.1)) and not g.is_legal(g.to_units(999.95))


def test_min_tick_mismatch_is_flagged_not_fatal():
    g = PriceGrid.from_market_rule(0.5, [(0, 0.25)])
    assert not g.min_tick_matches_rule
    assert g.unit == Decimal("0.25")


@pytest.mark.parametrize("args", [
    (0, [(0, 0.25)]),
    (-0.25, []),
    (0.25, [(0, 0)]),
    (0.25, [(0, 0.25), (0, 0.5)]),
    (0.25, [(10, 0.25), (5, 0.5)]),
    (0.25, [(-1, 0.25)]),
    (math.nan, []),
])
def test_invalid_grids(args):
    with pytest.raises(PriceGridError):
        PriceGrid(*args)


def test_empty_market_rule_rejected():
    with pytest.raises(PriceGridError):
        PriceGrid.from_market_rule(0.25, [])


@given(st.integers(min_value=-10**9, max_value=10**9))
def test_roundtrip_units(n):
    assert MNQ.to_units(MNQ.to_price(n)) == n


@given(st.integers(min_value=0, max_value=10**7))
def test_uniform_stepping_consistent(n):
    assert MNQ.next_down(MNQ.next_up(n)) == n
    assert MNQ.ticks_between(n, MNQ.next_up(n)) == 1


# ---------------------------------------------------------------------------
# Market rule selection (validExchanges <-> marketRuleIds)
# ---------------------------------------------------------------------------

def test_rule_selection():
    assert market_rule_id_for_exchange("CME,QBALGO", "67,67", "CME") == 67
    assert market_rule_id_for_exchange("SMART, CME", "26, 67", "cme") == 67


@pytest.mark.parametrize("ve,ids,ex", [
    ("CME,QBALGO", "67", "CME"),       # misaligned
    ("", "", "CME"),
    ("QBALGO", "67", "CME"),           # not listed
    ("CME,CME", "1,2", "CME"),         # conflicting
    ("CME", "x", "CME"),
])
def test_rule_selection_errors(ve, ids, ex):
    with pytest.raises(MarketRuleError):
        market_rule_id_for_exchange(ve, ids, ex)
