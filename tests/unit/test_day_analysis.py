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


def test_fast_replay_skipping_depth_gives_identical_bars(tmp_path):
    import random

    from hermes.config import DEFAULT_CONFIG_PATH, load_config
    from tests.support import BBO, DEPTH, TRADES, RawScript, write_hrec
    from tools.day_analysis import load_bars
    r1 = 1790083800
    sc = RawScript(wall0=(r1 - 600) * 10**9)
    sc.bootstrap(trading_hours="20260921:1700-20260922:1600", liquid_hours="20260922:0830-20260922:1500",
                 time_zone_id="US/Central")
    sc.seed_book()
    rng, t, px = random.Random(1), float(r1 - 600), 21000.0
    while t < r1 + 1800:
        t += rng.expovariate(4.0)
        sc.at(t)
        if rng.random() < 0.3:
            sc.tick()
        r = rng.random()
        if r < 0.6:
            sc.depth(DEPTH, rng.randint(0, 4), 1, rng.randint(0, 1), px + 0.25 * rng.randint(-4, 4), rng.randint(1, 9))
        elif r < 0.8:
            px = round((px + rng.gauss(0, 0.5)) * 4) / 4
            sc.bbo(BBO, px - 0.25, px)
        else:
            sc.trade(TRADES, px if rng.random() < 0.5 else px - 0.25, rng.randint(1, 5))
    sc.at(t + 400)
    sc.tick()
    write_hrec(tmp_path, sc.events)
    cfg = load_config(DEFAULT_CONFIG_PATH)
    full, _ = load_bars([tmp_path], cfg, with_depth=True)
    fast, _ = load_bars([tmp_path], cfg, with_depth=False)
    assert full and full == fast
