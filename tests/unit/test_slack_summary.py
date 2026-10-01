"""Trader-facing Slack message: concise, decision-oriented, deterministic, never fabricated."""

from __future__ import annotations

import dataclasses
import json

import pytest

from hermes.decision.approval import current_approval_payload
from hermes.decision.candidate import Vote
from hermes.decision.reasons import Reason, Severity
from hermes.slack.enrichment import luna_lines, screenshot_caption, sol_lines
from hermes.slack.protocol import ENTER_ACTION_ID, REJECT_ACTION_ID, SlackWorkflowState
from hermes.slack.render import render_slack_message, render_text
from hermes.slack.summary import (
    HISTORY_MIN_SAMPLES,
    HistoryStat,
    dominance,
    summarize,
    volume_intensity,
)
from tests.support import DEPTH, TICK
from tests.unit.test_candidate import T0, S, trend
from tests.unit.test_lifecycle import Live, started

NOW = (T0 + 605) * S

GOLDEN_LONG = """*🟢 LONG MNQZ6 · Entry 21010.25*
SL 21000.25 · Risk 10 pt ($20)

5m ▲ +9.75 · 1m ▲ +3.00 · 30s ▲ +0.50
VWAP above RTH · Volume NORMAL · Buyers 86%
Tape ✓ · OFI – · Sweep –

1) Aggressive buying
2) 5m/1m/30s aligned
3) Above RTH VWAP

[ ENTER ]  [ REJECT ]

Intent only — no order sent · valid 25 s"""


@pytest.fixture(scope="module")
def long_view():
    lv, rec = started()
    return current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)


def _buttons(msg):
    return [e["action_id"] for b in msg.blocks if b["type"] == "actions" for e in b["elements"]]


def _with_votes(p, **votes):
    ev = tuple(dataclasses.replace(c, vote=Vote(votes[c.name])) if c.name in votes else c
               for c in p.orderflow_evidence)
    return dataclasses.replace(p, orderflow_evidence=ev)


# ============================================================================ layout

def test_golden_long_message(long_view):
    assert render_text(render_slack_message(long_view)) == GOLDEN_LONG


def test_render_is_deterministic_and_short(long_view):
    a, b = render_slack_message(long_view), render_slack_message(long_view)
    assert a == b
    text = render_text(a)
    assert len(text) < 450 and len(text.splitlines()) <= 16


def test_no_internal_ids_codes_or_metadata_are_visible(long_view):
    text = render_text(render_slack_message(long_view))
    for s in (long_view.setup_id, long_view.proposal_id, long_view.approval_view_id, "Take-profit",
              "market-by-price", "MBP", "BLOCKERS", "lifecycle", "approval_allowed", "evaluation", "lag", "units"):
        assert s not in text, s
    for r in long_view.reasons:
        assert r.code not in text and f"{r.source}/{r.code}" not in text


def test_short_proposal_reads_naturally():
    lv = Live(trend(-1, minutes=10))
    rec = lv.lc.latest()
    p = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
    text = render_text(render_slack_message(p))
    assert text.startswith("*🔴 SHORT MNQZ6 · Entry ")
    assert "5m ▼" in text and "Aggressive selling" in text and "Sellers" in text


# ============================================================================ classifications

@pytest.mark.parametrize("trigger_total,expected", [(5, "LOW"), (21, "NORMAL"), (40, "HIGH")])
def test_volume_intensity_vs_setup_window(long_view, trigger_total, expected):
    t = dataclasses.replace(long_view.trigger_30s, buy_volume=trigger_total, sell_volume=0, unknown_volume=0)
    assert volume_intensity(dataclasses.replace(long_view, trigger_30s=t)) == f"Volume {expected}"


def test_volume_na_without_setup_volume(long_view):
    s = dataclasses.replace(long_view.setup_1m, buy_volume=0, sell_volume=0, unknown_volume=0)
    assert volume_intensity(dataclasses.replace(long_view, setup_1m=s)) == "Volume n/a"


@pytest.mark.parametrize("buy,sell,unknown,expected", [
    (86, 14, 500, "Buyers 86%"), (20, 80, 0, "Sellers 80%"), (52, 48, 0, "Balanced 52/48"), (0, 0, 9, "Flow n/a")])
def test_dominance_uses_known_volume_only(long_view, buy, sell, unknown, expected):
    s = dataclasses.replace(long_view.setup_1m, buy_volume=buy, sell_volume=sell, unknown_volume=unknown)
    assert dominance(dataclasses.replace(long_view, setup_1m=s)) == expected


def test_vwap_na_when_no_rth_vwap_condition(long_view):
    r5 = dataclasses.replace(long_view.regime_5m, conditions=tuple(
        c for c in long_view.regime_5m.conditions if "vwap" not in c[0]))
    s = summarize(dataclasses.replace(long_view, regime_5m=r5))
    assert s.context.startswith("VWAP n/a") and "Above RTH VWAP" not in s.reasons


def test_confirmation_marks(long_view):
    p = _with_votes(long_view, trade_flow="LONG", ofi="SHORT", sweep_follow="NEUTRAL")
    assert summarize(p).confirmation == "Tape ✓ · OFI ✗ · Sweep –"


def test_at_most_three_reasons_strongest_first(long_view):
    p = _with_votes(long_view, trade_flow="LONG", ofi="LONG", sweep_follow="LONG")
    assert summarize(p).reasons == ("Aggressive buying", "5m/1m/30s aligned", "Above RTH VWAP")
    r5 = dataclasses.replace(long_view.regime_5m, result="NEUTRAL")
    q = _with_votes(dataclasses.replace(long_view, regime_5m=r5), trade_flow="NEUTRAL", ofi="LONG", sweep_follow="LONG")
    assert summarize(q).reasons == ("Above RTH VWAP", "Sweep with follow-through", "Book pressure confirms (OFI)")


# ============================================================================ warnings (decisional only)

def test_no_warning_section_when_nothing_decisional(long_view):
    assert summarize(long_view).warnings == ()
    assert "⚠" not in render_text(render_slack_message(long_view))


def test_flow_conflicts_are_warned(long_view):
    p = _with_votes(long_view, absorption_compatible="SHORT")
    assert summarize(p).warnings == ("⚠ Flow conflict: sellers absorbing buys at ask",)
    p2 = _with_votes(long_view, trade_flow="SHORT", sweep_follow="SHORT", ofi="SHORT")
    w = summarize(p2).warnings
    assert len(w) == 2 and w[0] == "⚠ Flow conflict: aggressive selling on tape"


def test_unknown_dominated_trigger_is_warned(long_view):
    extra = Reason("trigger_30s", "trigger_bar_mostly_unknown_aggressor", "x", Severity.CAUTION)
    p = dataclasses.replace(long_view, reasons=long_view.reasons + (extra,))
    assert "⚠ Trigger volume mostly UNKNOWN aggressor" in summarize(p).warnings


# ============================================================================ history: never fabricated

def test_history_absent_without_recorded_source(long_view):
    assert summarize(long_view).history is None
    assert "History" not in render_text(render_slack_message(long_view))


def _hist(wins: int, samples: int) -> HistoryStat:
    return HistoryStat(wins=wins, samples=samples, outcome_definition="+1R before stop, same RTH session",
                       selection_definition="LONG continuation, 5m/1m/30s aligned, trade-flow confirmed",
                       source="hermes recordings 2026-09 (replayed)")


def test_history_shown_with_sample_count_when_fully_defined(long_view):
    text = render_text(render_slack_message(long_view, history=_hist(34, 50)))
    assert "History: 68% win · 50 similar setups" in text


def test_history_requires_at_least_30_comparable_setups(long_view):
    assert HISTORY_MIN_SAMPLES == 30
    assert summarize(long_view, _hist(20, 29)).history is None                 # 29 setups: never shown
    assert summarize(long_view, _hist(20, 30)).history == "History: 67% win · 30 similar setups"


@pytest.mark.parametrize("h", [
    HistoryStat(10, 12, "+1R", "cont.", "rec"),              # too few samples
    HistoryStat(20, 29, "+1R", "cont.", "rec"),              # still below 30
    HistoryStat(30, 42, "", "cont.", "rec"),                 # outcome not defined
    HistoryStat(30, 42, "+1R", " ", "rec"),                  # comparable set not defined
    HistoryStat(30, 42, "+1R", "cont.", ""),                 # no recorded source
    HistoryStat(50, 42, "+1R", "cont.", "rec"),              # impossible counts
])
def test_history_never_displayed_unless_backed(long_view, h):
    assert summarize(long_view, h).history is None


def test_bridge_never_passes_a_history_source():
    import inspect

    from hermes.slack import bridge
    src = inspect.getsource(bridge)
    assert "history=" not in src and "HistoryStat" not in src


# ============================================================================ states

def test_hold_shows_one_status_line_and_no_enter():
    lv, rec = started()
    lv.at(605)
    lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)
    lv.pump()
    held = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    msg = render_slack_message(held, workflow_state=SlackWorkflowState.HOLD,
                               banner="Temporary market-data hold — ENTER unavailable.")
    text = render_text(msg)
    assert "⏸ Temporary market-data hold — ENTER unavailable. (crossed book)" in text
    assert _buttons(msg) == [REJECT_ACTION_ID]


def test_terminal_and_closed_states_have_no_buttons():
    lv, rec = started()
    lv.tick_until(630)
    exp = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
    msg = render_slack_message(exp, workflow_state=SlackWorkflowState.CLOSED)
    text = render_text(msg)
    assert _buttons(msg) == [] and "🔒 Expired — proposal ended, ENTER disabled." in text
    assert text.endswith("Intent only — no order sent")                  # no "valid N s" once ended


def test_enter_recorded_banner(long_view):
    msg = render_slack_message(long_view, workflow_state=SlackWorkflowState.ENTER_RECORDED,
                               banner="ENTER intent recorded. No order was sent; execution remains disabled.")
    assert "✅ ENTER intent recorded. No order was sent" in render_text(msg) and _buttons(msg) == []


def test_buttons_still_carry_the_exact_view_ids(long_view):
    msg = render_slack_message(long_view)
    vals = [json.loads(e["value"]) for b in msg.blocks if b["type"] == "actions" for e in b["elements"]]
    assert _buttons(msg) == [ENTER_ACTION_ID, REJECT_ACTION_ID]
    assert all(v == {"s": long_view.setup_id, "p": long_view.proposal_id, "v": long_view.approval_view_id}
               for v in vals)


# ============================================================================ screenshot caption

def test_screenshot_caption_is_one_line_without_ai(long_view):
    assert screenshot_caption(long_view) == "📸 TWS · MNQZ6 LONG @ 21010.25"


def test_luna_keeps_only_two_useful_lines():
    raw = ("VISUAL: chart shows uptrend\nALIGNMENT: higher lows on 1m, bids stacking\n"
           "CONFLICT: NONE\nDATA QUALITY: ok\nNOTE: blah\nCONFLICT: offer wall 21012")
    assert luna_lines(raw) == ["ALIGNMENT: higher lows on 1m, bids stacking", "CONFLICT: offer wall 21012"]
    assert luna_lines("ALIGNMENT: UNKNOWN\nCONFLICT: none") == []


def test_sol_only_when_it_reports_a_problem(long_view):
    assert sol_lines("NONE") == [] and sol_lines(" none. ") == [] and sol_lines(None) == []
    assert sol_lines("5m regime conflicts with 30s trigger\nsecond\nthird") == [
        "5m regime conflicts with 30s trigger", "second"]
    cap = screenshot_caption(long_view, "ALIGNMENT: bids stacking\nCONFLICT: NONE", "NONE")
    assert cap == "📸 TWS · MNQZ6 LONG @ 21010.25\n🧠 ALIGNMENT: bids stacking"


GOLDEN_FULL = """*🟢 LONG MNQZ6 · Entry 21010.25*
SL 21000.25 · Risk 10 pt ($20)

5m ▲ +9.75 · 1m ▲ +3.00 · 30s ▲ +0.50
VWAP above RTH · Volume HIGH · Buyers 86%
Tape ✓ · OFI ✓ · Sweep –

1) Aggressive buying
2) 5m/1m/30s aligned
3) Above RTH VWAP

⚠ Flow conflict: sellers absorbing buys at ask

History: 68% win · 50 similar setups

[ ENTER ]  [ REJECT ]

Intent only — no order sent · valid 25 s"""


def test_golden_full_message_with_warning_and_history(long_view):
    """Every optional line present only because it is justified (conflict + recorded history)."""
    t = dataclasses.replace(long_view.trigger_30s, buy_volume=40, sell_volume=0, unknown_volume=0)
    p = _with_votes(dataclasses.replace(long_view, trigger_30s=t), ofi="LONG", absorption_compatible="SHORT")
    assert render_text(render_slack_message(p, history=_hist(34, 50))) == GOLDEN_FULL
