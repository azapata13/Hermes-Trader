from datetime import datetime, timedelta

from tools.day_analysis import NY, B, synthetic_day
from tools.scalp_lab import SETUPS, Bracket, build_ctx, run_bracket, simulate_day, summarize, verdict

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=NY)


def bars(ohlc, start=T0):
    return [B(start + timedelta(minutes=i), o, h, l, c, 10, 5, 5, 10 * c) for i, (o, h, l, c) in enumerate(ohlc)]


def test_target_hit_long():
    b = bars([(100, 105, 99, 104), (104, 111, 103, 110)])
    k, px, out, amb = run_bracket(b, 0, 1, 100.0, Bracket(10, 10, False), 0)
    assert (k, px, out, amb) == (1, 110.0, "OBJECTIF", False)


def test_stop_hit_short_and_gap_fill():
    b = bars([(100, 104, 99, 103), (115, 116, 114, 115)])
    k, px, out, _ = run_bracket(b, 0, -1, 100.0, Bracket(10, 10, False), 0)
    assert out == "STOP" and px == 115.0          # gapped through 110: filled at the open


def test_target_and_stop_same_bar_counts_as_loss():
    b = bars([(100, 111, 89, 100)])
    _, px, out, amb = run_bracket(b, 0, 1, 100.0, Bracket(10, 10, False), 0)
    assert out == "STOP" and px == 90.0 and amb


def test_break_even_move():
    b = bars([(100, 106.5, 99, 106), (106, 106, 99.5, 100)])
    _, px, out, _ = run_bracket(b, 0, 1, 100.0, Bracket(10, 10, True), 0)
    assert out == "BE" and px == 100.25


def test_forced_exit_before_close():
    b = bars([(100, 101, 99, 100), (100, 101, 99, 100)], start=datetime(2026, 11, 2, 15, 54, tzinfo=NY))
    k, px, out, _ = run_bracket(b, 0, 1, 100.0, Bracket(10, 10, False), 0)
    assert out == "TEMPS" and k == 1


def test_signals_never_look_ahead():
    day, _ = synthetic_day()
    ctx = build_ctx(day, None)
    cut = 200
    trunc = build_ctx(day[:cut + 1], None)
    for name, fn in SETUPS.items():
        a = [fn(ctx, i, {}) for i in range(cut + 1)]
        b = [fn(trunc, i, {}) for i in range(cut + 1)]
        assert a == b, name


def test_summary_and_verdict_shape():
    day, _ = synthetic_day()
    trades = simulate_day(build_ctx(day, None), "2026-11-02", 0.75)
    rows = summarize(trades, 0.75)
    assert rows and all(0 <= r["win_rate"] <= 1 for r in rows)
    ok, _ = verdict(rows, 5)
    assert all(r["win_rate"] >= 0.6 and r["expectancy"] > 0 for r in ok)
