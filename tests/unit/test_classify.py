"""TradeClassifier: quote rules, quote-move ambiguity, tick rule, eligibility (C4)."""

from __future__ import annotations

import dataclasses

import pytest

from hermes.config import TapeConfig
from hermes.market.classify import Aggressor, ClassMethod, QuoteState, TradeClassifier, UnknownReason

MS = 1_000_000
BUY, SELL, UNK = Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN
G = 7          # BBO generation
TG = 9         # trades generation


def quote(bid, ask, t_ms, seq=0, gen=G):
    return QuoteState(bid, ask, 5, 5, seq, gen, t_ms * MS, t_ms * MS, 0)


def clf(**kw) -> TradeClassifier:
    return TradeClassifier(dataclasses.replace(TapeConfig(), **kw))


def classify(c, price, t_ms, size=1, past_limit=False, unreported=False, special="", ok=True, gen=TG):
    return c.classify(price, size, past_limit, unreported, special, gen, t_ms * MS, ok)


# ---------------------------------------------------------------------------
# Primary quote rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price,side", [(101, BUY), (103, BUY), (100, SELL), (97, SELL)])
def test_at_or_beyond_prevailing_quote(price, side):
    c = clf()
    c.on_quote(quote(100, 101, 0, seq=1))
    r = classify(c, price, 1000)
    assert (r.aggressor, r.method, r.confidence, r.reason) == (side, ClassMethod.DIRECT_QUOTE, 1.0, None)
    assert r.ref_quote_seq == 1 and r.quote.seq == 1


def test_no_quote_and_one_sided_quote_are_unknown():
    c = clf()
    r = classify(c, 100, 0)
    assert (r.aggressor, r.reason) == (UNK, UnknownReason.NO_QUOTE)
    c.on_quote(quote(None, 101, 0))
    assert classify(c, 101, 1).reason is UnknownReason.NO_QUOTE


@pytest.mark.parametrize("bid,ask", [(100, 100), (101, 100)])
def test_locked_or_crossed_quote_is_unknown(bid, ask):
    c = clf()
    c.on_quote(quote(bid, ask, 0))
    r = classify(c, 100, 1)
    assert (r.aggressor, r.method, r.reason) == (UNK, ClassMethod.NONE, UnknownReason.LOCKED_OR_CROSSED)


def test_invalid_context_is_unknown_but_tick_state_advances():
    c = clf()
    c.on_quote(quote(100, 101, 0))
    r = classify(c, 101, 1, ok=False)
    assert (r.aggressor, r.reason) == (UNK, UnknownReason.INVALID_CONTEXT)
    assert c.tick_reference == 101


def test_stale_quote_optional():
    c = clf(max_quote_age_ms=500)
    c.on_quote(quote(100, 101, 0))
    assert classify(c, 101, 500).aggressor is BUY
    assert classify(c, 101, 501).reason is UnknownReason.STALE_QUOTE
    c2 = clf()                                       # default: disabled (quiet BBO is legitimate)
    c2.on_quote(quote(100, 101, 0))
    assert classify(c2, 101, 3_600_000).aggressor is BUY


# ---------------------------------------------------------------------------
# Quote moved before the trade callback reached us
# ---------------------------------------------------------------------------

def test_quote_moves_up_before_trade_callback_old_ask_lifted():
    """old 100.00/100.25, trade 100.25, new 100.25/100.50 arrives first: not a SELL."""
    c = clf()
    c.on_quote(quote(100, 101, 0, seq=1))
    c.on_quote(quote(101, 102, 10, seq=2))           # moved up before the trade callback
    r = classify(c, 101, 12)
    assert (r.aggressor, r.method, r.confidence) == (BUY, ClassMethod.HISTORICAL_QUOTE, 0.6)
    assert r.ref_quote_seq == 1 and r.quote.seq == 2


def test_quote_moves_down_before_trade_callback_old_bid_hit():
    c = clf()
    c.on_quote(quote(100, 101, 0, seq=1))
    c.on_quote(quote(99, 100, 10, seq=2))
    r = classify(c, 100, 12)
    assert (r.aggressor, r.method, r.ref_quote_seq) == (SELL, ClassMethod.HISTORICAL_QUOTE, 1)


def test_history_outside_window_is_not_used():
    c = clf(ambiguity_window_ms=250)
    c.on_quote(quote(100, 101, 0, seq=1))
    c.on_quote(quote(101, 102, 10, seq=2))
    r = classify(c, 101, 300)                        # old quote is 300 ms before the trade
    assert (r.aggressor, r.method) == (SELL, ClassMethod.DIRECT_QUOTE)


def test_stable_quote_updates_do_not_create_ambiguity():
    c = clf()
    for i in range(10):
        c.on_quote(quote(100, 101, i, seq=i))        # size-only updates of the same quote
    r = classify(c, 101, 11)
    assert (r.aggressor, r.method) == (BUY, ClassMethod.DIRECT_QUOTE)


def test_quote_oscillating_through_the_level_is_ambiguous():
    c = clf()
    c.on_quote(quote(100, 101, 0, seq=1))            # 101 = ask (BUY)
    c.on_quote(quote(101, 102, 5, seq=2))            # 101 = bid (SELL)
    c.on_quote(quote(100, 101, 6, seq=3))            # back: 101 = ask (BUY) by current quote
    r = classify(c, 101, 7)
    assert (r.aggressor, r.method, r.reason, r.confidence) == (UNK, ClassMethod.NONE, UnknownReason.AMBIGUOUS, 0.0)


def test_inside_spread_resolved_by_single_sided_history():
    c = clf()
    c.on_quote(quote(99, 100, 0, seq=1))             # 100 = old ask
    c.on_quote(quote(98, 102, 5, seq=2))             # widened: 100 strictly inside now
    r = classify(c, 100, 6)
    assert (r.aggressor, r.method, r.ref_quote_seq) == (BUY, ClassMethod.HISTORICAL_QUOTE, 1)


def test_inside_spread_with_history_on_both_sides_is_ambiguous():
    c = clf()
    c.on_quote(quote(96, 100, 0, seq=1))             # 100 = old ask
    c.on_quote(quote(100, 104, 5, seq=2))            # 100 = old bid
    c.on_quote(quote(98, 102, 6, seq=3))             # now strictly inside the spread
    r = classify(c, 100, 7)
    assert (r.aggressor, r.reason) == (UNK, UnknownReason.AMBIGUOUS)


def test_history_ignores_other_generations():
    c = clf()
    c.on_quote(quote(100, 101, 0, seq=1, gen=1))
    c.on_quote(quote(101, 102, 10, seq=2, gen=2))    # new generation clears history
    r = classify(c, 101, 12)
    assert (r.aggressor, r.method) == (SELL, ClassMethod.DIRECT_QUOTE)
    assert c.quote_history_len == 1


# ---------------------------------------------------------------------------
# Inside spread / tick rule
# ---------------------------------------------------------------------------

def test_inside_spread_uses_tick_rule_low_confidence():
    c = clf()
    c.on_quote(quote(100, 104, 0))
    assert classify(c, 102, 1).reason is UnknownReason.NO_TICK_REFERENCE     # no previous eligible trade
    r = classify(c, 103, 2)
    assert (r.aggressor, r.method, r.confidence) == (BUY, ClassMethod.TICK_RULE, 0.3)
    assert classify(c, 101, 3).aggressor is SELL
    r = classify(c, 101, 4)                                                   # zero tick: keep last direction
    assert (r.aggressor, r.method) == (SELL, ClassMethod.TICK_RULE)


def test_tick_rule_zero_tick_without_prior_direction_is_unknown():
    c = clf()
    c.on_quote(quote(100, 104, 0))
    classify(c, 102, 1)
    r = classify(c, 102, 2)
    assert (r.aggressor, r.reason) == (UNK, UnknownReason.NO_TICK_REFERENCE)


def test_tick_state_updates_from_quote_classified_trades_and_resets_per_generation():
    c = clf()
    c.on_quote(quote(100, 104, 0))
    classify(c, 104, 1)                              # BUY by quote, also sets tick reference 104
    assert classify(c, 103, 2).aggressor is SELL     # inside spread, downtick vs 104
    assert classify(c, 102, 3, gen=TG + 1).reason is UnknownReason.NO_TICK_REFERENCE  # new trades generation
    c.reset_tick()
    assert c.tick_reference is None and c.tick_direction is None


# ---------------------------------------------------------------------------
# Eligibility policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [{"past_limit": True}, {"unreported": True}, {"special": "X"}, {"size": 0}])
def test_ineligible_prints_are_unknown_and_do_not_touch_tick_state(kw):
    c = clf()
    c.on_quote(quote(100, 101, 0))
    r = classify(c, 101, 1, **kw)
    assert (r.aggressor, r.reason, r.eligible) == (UNK, UnknownReason.INELIGIBLE, False)
    assert c.tick_reference is None


def test_eligibility_is_configurable():
    c = clf(allowed_special_conditions="A, B", classify_past_limit=True, classify_unreported=True)
    c.on_quote(quote(100, 101, 0))
    assert classify(c, 101, 1, special="A,B").aggressor is BUY
    assert classify(c, 101, 2, special="A,C").reason is UnknownReason.INELIGIBLE
    assert classify(c, 101, 3, past_limit=True).aggressor is BUY
    assert classify(c, 101, 4, unreported=True).aggressor is BUY


def test_classifier_is_deterministic():
    def run():
        c = clf()
        out = []
        for i in range(200):
            c.on_quote(quote(100 + i % 3, 101 + i % 3, i * 3, seq=i))
            out.append(classify(c, 100 + (i * 7) % 5, i * 3 + 1))
        return out
    assert run() == run()


# ---------------------------------------------------------------------------
# C4 baseline window (50 ms, provisional) and explicit method/confidence/age
# ---------------------------------------------------------------------------

def test_default_window_is_50ms_baseline():
    assert TapeConfig().ambiguity_window_ms == 50
    c = clf()                                        # default config
    c.on_quote(quote(100, 101, 0, seq=1))
    c.on_quote(quote(101, 102, 10, seq=2))
    r = classify(c, 101, 50)                         # old quote exactly 50 ms before: inside the window
    assert (r.aggressor, r.method, r.confidence) == (BUY, ClassMethod.HISTORICAL_QUOTE, 0.6)
    assert r.ref_quote_seq == 1 and r.ref_quote_age_ns == 50 * MS
    c = clf()
    c.on_quote(quote(100, 101, 0, seq=1))
    c.on_quote(quote(101, 102, 10, seq=2))
    r = classify(c, 101, 51)                         # 51 ms: outside -> prevailing quote only
    assert (r.aggressor, r.method, r.confidence, r.ref_quote_age_ns) == (SELL, ClassMethod.DIRECT_QUOTE, 1.0, 41 * MS)


def test_every_method_keeps_its_explicit_label_and_confidence():
    c = clf()
    c.on_quote(quote(100, 104, 0, seq=1))
    direct = classify(c, 104, 1)
    tick_ref = classify(c, 103, 2)                   # inside spread, downtick vs 104
    assert (direct.method, direct.confidence) == (ClassMethod.DIRECT_QUOTE, 1.0)
    assert (tick_ref.method, tick_ref.confidence, tick_ref.ref_quote_age_ns) == (ClassMethod.TICK_RULE, 0.3, None)
    unk = classify(c, 101, 3, special="Z")
    assert (unk.method, unk.confidence, unk.aggressor) == (ClassMethod.NONE, 0.0, UNK)
    assert [m.value for m in ClassMethod] == ["direct_quote", "historical_quote", "tick_rule", "none"]
