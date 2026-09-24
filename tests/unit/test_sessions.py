"""Session calendar (tradingHours/liquidHours, zoneinfo, DST 2026) and session context (C5)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hermes.market.sessions import SessionCalendar, SessionTracker, load_zone, local_to_utc_s

S = 1_000_000_000


def utc(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


# MNQ-like week around the 2026 fall DST transition (US: Sun 2026-11-01 02:00 CDT -> 01:00 CST)
FALL_TRADING = ("20261029:1700-20261030:1600;20261031:CLOSED;20261101:1700-20261102:1600;"
                "20261102:1700-20261103:1600")
FALL_LIQUID = "20261030:0830-20261030:1500;20261031:CLOSED;20261102:0830-20261102:1500"


# ---------------------------------------------------------------------------- parsing

def test_current_format_crossing_midnight_and_trading_date():
    cal = SessionCalendar("US/Central", "20260921:1700-20260922:1600;20260922:1700-20260923:1600", "")
    assert cal.valid, cal.error
    a, b = cal.trading
    assert (a.start_s, a.end_s) == (utc("2026-09-21T22:00:00"), utc("2026-09-22T21:00:00"))   # CDT = UTC-5
    assert a.trading_date == "20260922"          # Sunday/evening open belongs to the next trading date
    assert b.trading_date == "20260923"
    assert cal.trading_at(utc("2026-09-22T21:30:00")) is None          # CME daily maintenance break
    assert cal.next_trading_start(utc("2026-09-22T21:30:00")) == utc("2026-09-22T22:00:00")


def test_legacy_format_same_day_and_midnight_and_multiple_ranges():
    cal = SessionCalendar("America/Chicago", "20260921:1700-1600;20260923:0830-1200,1300-1500", "")
    assert cal.valid, cal.error
    w0, w1, w2 = cal.trading
    assert (w0.start_s, w0.end_s) == (utc("2026-09-21T22:00:00"), utc("2026-09-22T21:00:00"))  # crosses midnight
    assert (w1.start_s, w1.end_s) == (utc("2026-09-23T13:30:00"), utc("2026-09-23T17:00:00"))
    assert w2.start_s == utc("2026-09-23T18:00:00")


def test_closed_days_dedupe_and_merge():
    cal = SessionCalendar("US/Central", "20260926:CLOSED;20260927:1700-20260928:1600;"
                                        "20260927:1700-20260928:1600", "")
    assert len(cal.trading) == 1


@pytest.mark.parametrize("tz,hours", [("Mars/Olympus", "20260921:1700-20260922:1600"),
                                       ("US/Central", "garbage"), ("US/Central", ""),
                                       ("US/Central", "20260921:1700-20260921:1600")])
def test_invalid_calendar_is_unknown_not_guessed(tz, hours):
    cal = SessionCalendar(tz, hours, "")
    assert not cal.valid and cal.error and cal.trading == ()
    assert cal.trading_at(utc("2026-09-22T00:00:00")) is None


def test_ibkr_zone_aliases_have_dst_rules():
    assert load_zone("CST").key == "America/Chicago"      # zoneinfo "CST"-style keys would be wrong/fixed
    assert load_zone("US/Central").utcoffset(datetime(2026, 7, 1)).total_seconds() == -5 * 3600


# ---------------------------------------------------------------------------- DST (2026 fixtures)

def test_dst_fall_2026_mnq_week():
    cal = SessionCalendar("US/Central", FALL_TRADING, FALL_LIQUID)
    assert cal.valid, cal.error
    thu, sun, mon = cal.trading
    assert (thu.start_s, thu.end_s) == (utc("2026-10-29T22:00:00"), utc("2026-10-30T21:00:00"))   # CDT
    assert (sun.start_s, sun.end_s) == (utc("2026-11-01T23:00:00"), utc("2026-11-02T22:00:00"))   # CST
    assert mon.start_s == utc("2026-11-02T23:00:00")
    assert (thu.trading_date, sun.trading_date, mon.trading_date) == ("20261030", "20261102", "20261103")
    rth_fri, rth_mon = cal.liquid
    assert (rth_fri.start_s, rth_fri.end_s) == (utc("2026-10-30T13:30:00"), utc("2026-10-30T20:00:00"))
    assert (rth_mon.start_s, rth_mon.end_s) == (utc("2026-11-02T14:30:00"), utc("2026-11-02T21:00:00"))
    assert cal.liquid_in(sun) == rth_mon
    assert cal.anomalies == {}


def test_session_spanning_fall_back_is_25h_and_spring_forward_is_23h():
    fall = SessionCalendar("US/Central", "20261031:1600-20261101:1700", "").trading[0]
    spring = SessionCalendar("US/Central", "20260307:1600-20260308:1700", "").trading[0]
    assert fall.end_s - fall.start_s == 26 * 3600         # 25 h wall + 1 h repeated
    assert spring.end_s - spring.start_s == 24 * 3600     # 25 h wall - 1 h skipped


def test_ambiguous_local_time_resolves_inclusively():
    tz = load_zone("US/Central")
    an: dict[str, int] = {}
    start = local_to_utc_s(2026, 11, 1, 1, 30, tz, "start", an)
    end = local_to_utc_s(2026, 11, 1, 1, 30, tz, "end", an)
    assert start == utc("2026-11-01T06:30:00")            # first 01:30 (CDT)
    assert end == utc("2026-11-01T07:30:00")              # second 01:30 (CST)
    assert an == {"ambiguous": 2}
    cal = SessionCalendar("US/Central", "20261101:0130-20261101:0130", "")
    assert cal.valid and cal.trading[0].end_s - cal.trading[0].start_s == 3600
    assert dict(cal.anomalies) == {"ambiguous": 2}


def test_nonexistent_local_time_shifts_forward_by_gap():
    tz = load_zone("US/Central")
    an: dict[str, int] = {}
    t = local_to_utc_s(2026, 3, 8, 2, 30, tz, "start", an)
    assert t == utc("2026-03-08T08:30:00")                # 02:30 does not exist -> 03:30 CDT
    assert an == {"nonexistent": 1}


def test_calendar_is_deterministic():
    a = SessionCalendar("US/Central", FALL_TRADING, FALL_LIQUID)
    b = SessionCalendar("US/Central", FALL_TRADING, FALL_LIQUID)
    assert a.trading == b.trading and a.liquid == b.liquid


# ---------------------------------------------------------------------------- session context

WEEK = "20260920:1700-20260921:1600;20260921:1700-20260922:1600;20260922:1700-20260923:1600"
WEEK_RTH = "20260921:0830-20260921:1500;20260922:0830-20260922:1500"


def tracker(start_iso="2026-09-20T21:00:00"):
    t = SessionTracker(500)
    t.set_calendar(SessionCalendar("US/Central", WEEK, WEEK_RTH))
    t.advance(utc(start_iso) * S)              # observing since before the Sunday open
    return t


def test_session_rth_overnight_vwap_and_integer_accumulators():
    t = tracker()
    t.advance(utc("2026-09-20T22:00:00") * S)
    snap = t.snapshot()
    assert snap.in_trading_session and not snap.in_rth and snap.trading_date == "20260921"
    assert snap.observed_from_open and snap.session.volume == 0
    t.on_trade(utc("2026-09-21T01:00:00"), 100, 2)            # overnight
    t.on_trade(utc("2026-09-21T05:00:00"), 90, 1)             # overnight low
    t.advance(utc("2026-09-21T13:30:00") * S)
    assert t.snapshot().in_rth
    t.on_trade(utc("2026-09-21T13:30:00"), 110, 3)            # RTH open
    t.on_trade(utc("2026-09-21T15:00:00"), 120, 1)
    s = t.snapshot()
    assert (s.session.open, s.session.high, s.session.low, s.session.last) == (100, 120, 90, 120)
    assert s.session.volume == 7 and s.session.vwap_num == 100 * 2 + 90 + 110 * 3 + 120
    assert s.session.vwap == pytest.approx((200 + 90 + 330 + 120) / 7)
    assert (s.rth.open, s.rth.high, s.rth.low, s.rth.volume) == (110, 120, 110, 4)
    assert (s.overnight.high, s.overnight.low, s.overnight.volume) == (100, 90, 3)
    assert s.previous is None                                 # nothing observed before: never fabricated


def test_vwap_resets_at_session_boundary_and_previous_is_observed_only():
    t = tracker()
    t.on_trade(utc("2026-09-21T01:00:00"), 100, 1)
    t.on_trade(utc("2026-09-21T20:59:59"), 104, 1)
    t.advance(utc("2026-09-21T21:00:00") * S)                 # close: grace not yet elapsed
    assert t.snapshot().session is not None
    t.advance(utc("2026-09-21T21:00:00") * S + S // 2)        # close + 500 ms grace
    s = t.snapshot()
    assert not s.in_trading_session and s.session is None
    assert (s.previous.high, s.previous.low, s.previous.close, s.previous.trading_date) == (104, 100, 104, "20260921")
    t.advance(utc("2026-09-21T22:00:00") * S)
    t.on_trade(utc("2026-09-21T22:00:01"), 200, 1)
    s = t.snapshot()
    assert s.session.vwap_num == 200 and s.session.volume == 1 and s.trading_date == "20260922"
    t.on_trade(utc("2026-09-21T20:59:59"), 999, 5)            # late print for the finished session
    assert t.snapshot().late_session_trades == 1 and t.snapshot().previous.high == 104
    t.on_trade(utc("2026-09-21T21:30:00"), 50, 1)             # during the maintenance break
    assert t.snapshot().trades_outside_session == 1


def test_started_mid_session_and_gap_flags():
    t = tracker(start_iso="2026-09-21T12:00:00")              # process started mid-session
    t.advance(utc("2026-09-21T12:00:00") * S)
    assert not t.snapshot().observed_from_open
    t.set_gap(True)
    assert t.snapshot().gap_observed
    t.set_gap(False)
    assert t.snapshot().gap_observed                          # sticky for the session
    t.advance(utc("2026-09-21T22:00:00") * S)                 # next session starts clean
    s = t.snapshot()
    assert s.observed_from_open and not s.gap_observed and s.previous is None   # no trades seen before


def test_unknown_calendar_has_no_session_context():
    t = SessionTracker(500)
    t.set_calendar(SessionCalendar("US/Central", "", ""))
    t.advance(utc("2026-09-21T15:00:00") * S)
    t.on_trade(utc("2026-09-21T15:00:00"), 100, 1)
    s = t.snapshot()
    assert not s.calendar_ok and not s.in_trading_session and s.session is None
