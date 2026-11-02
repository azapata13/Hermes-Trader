from datetime import datetime, timedelta

from tools.day_analysis import NY, B, StopRule, analyse_day, simulate, stop_tests, synthetic_day

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=NY)


def bars(ohlc):
    return [B(T0 + timedelta(minutes=i), o, h, l, c, 10, 5, 5, 10 * c) for i, (o, h, l, c) in enumerate(ohlc)]


def test_fixed_stop_hit_long_with_slippage():
    p = bars([(100, 101, 95, 96), (96, 97, 85, 86)])
    r = simulate(p, 1, 100.0, StopRule("f", "fixed", 10), None, 0, 0.25)
    assert r["exit"] == "STOP" and r["pts"] == -10.25


def test_gap_through_stop_fills_at_open():
    p = bars([(80, 82, 79, 81)])
    r = simulate(p, 1, 100.0, StopRule("f", "fixed", 10), None, 0, 0.0)
    assert r["pts"] == -20.0


def test_trailing_stop_ratchets_and_short_side():
    p = bars([(100, 100, 80, 81), (81, 92, 80, 91)])   # short: best 80 -> stop 90, hit next bar
    r = simulate(p, -1, 100.0, StopRule("t", "trail", 10), None, 0, 0.0)
    assert r["exit"] == "STOP" and r["pts"] == 10.0


def test_trail_is_pessimistic_within_bar():
    # same bar makes a new high and dips: stop tested BEFORE the trail advances
    p = bars([(100, 130, 95, 128)])
    r = simulate(p, 1, 100.0, StopRule("t", "trail", 10), None, 0, 0.0)
    assert r["exit"] == "CLÔTURE" and r["pts"] == 28.0


def test_break_even_then_trail():
    p = bars([(100, 112, 100, 111), (111, 111, 99, 100)])
    r = simulate(p, 1, 100.0, StopRule("be", "be_trail", 15, 10), None, 0, 0.0)
    assert r["exit"] == "STOP" and r["pts"] == 0.0


def test_synthetic_day_report_is_deterministic():
    b, ctx = synthetic_day()
    a1, a2 = analyse_day(b, ctx), analyse_day(*synthetic_day())
    assert a1 == a2 and [s["name"] for s in a1["segments"]] == ["OUVERTURE", "MIDI", "FIN"]
    assert {t["name"] for t in stop_tests(b, 0.25)} >= {"MIDI 11:00", "FIN 14:00"}
