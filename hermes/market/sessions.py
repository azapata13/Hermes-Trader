"""Exchange session calendar and per-session market context (C5).

``SessionCalendar`` parses IBKR ``ContractDetails.tradingHours`` / ``liquidHours`` in the
contract's ``timeZoneId`` into UTC windows (epoch seconds). Both IBKR formats are accepted:

* current : ``20261101:1700-20261102:1600;20261031:CLOSED;...`` (explicit end date)
* legacy  : ``20261101:1700-1600,1700-2359;...`` (end on the start date, or the next day when
  the end time is not after the start time — i.e. the session crosses midnight)

DST is handled explicitly and deterministically (``zoneinfo``; the pinned ``tzdata`` package is
preferred over the host database so every machine resolves identical offsets):

* non-existent local times (spring-forward gap) are shifted forward by the gap length
  (standard ``fold=0`` semantics) and counted in ``anomalies["nonexistent"]``;
* ambiguous local times (fall-back overlap) resolve INCLUSIVELY: a window START takes the
  earlier instant and a window END the later one; counted in ``anomalies["ambiguous"]``.

Exchange trading date of a window = local date of its last second (CME: the Sunday 17:00 CT open
belongs to Monday's trading date). ``tradingHours`` = TradingSession, ``liquidHours`` = RTH.
Authorized entry hours (future ``UserTradingWindow``) are deliberately NOT modelled here.

``SessionTracker`` keeps per-trading-session context from bar-eligible prints only (so session
volume == the sum of the bars' volume): open / high / low / last-so-far, volume, integer VWAP
accumulators (``vwap_num = sum(price_units * size)``), RTH and overnight (pre-RTH part of the
trading session) statistics, and the PREVIOUS session only if it was actually observed. It reads
no clock: time comes from trade exchange timestamps and the engine's event-time watermark.
"""

from __future__ import annotations

import bisect
import importlib.resources
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Legacy/abbreviated zone names IBKR has used, mapped to IANA zones WITH DST rules
# (zoneinfo's own "EST"/"CST6CDT"-style keys are fixed-offset or legacy; be explicit).
_TZ_ALIASES = {
    "CST": "America/Chicago", "CDT": "America/Chicago", "CTT": "America/Chicago",
    "EST": "America/New_York", "EDT": "America/New_York",
    "MST": "America/Denver", "PST": "America/Los_Angeles",
    "GMT": "UTC", "UTC": "UTC",
}


def load_zone(name: str) -> ZoneInfo:
    """Resolve a zone, preferring the pinned ``tzdata`` package (deterministic across hosts)."""
    key = _TZ_ALIASES.get(name.strip(), name.strip())
    if not key:
        raise ZoneInfoNotFoundError("empty time zone id")
    try:
        res = importlib.resources.files("tzdata.zoneinfo").joinpath(*key.split("/"))
        with res.open("rb") as fh:
            return ZoneInfo.from_file(fh, key=key)
    except (ModuleNotFoundError, FileNotFoundError, IsADirectoryError, ValueError, OSError):
        return ZoneInfo(key)          # host database fallback (raises ZoneInfoNotFoundError if absent)


def local_to_utc_s(y: int, mo: int, d: int, hh: int, mm: int, tz: ZoneInfo, role: str,
                   anomalies: dict[str, int] | None = None) -> int:
    """Local wall time -> UTC epoch seconds with explicit DST policy (see module doc).

    ``role`` is ``"start"`` or ``"end"`` (only matters for ambiguous times).
    """
    naive = datetime(y, mo, d) + timedelta(hours=hh, minutes=mm)     # 24:00 == next midnight
    t0 = int(naive.replace(tzinfo=tz, fold=0).timestamp())
    t1 = int(naive.replace(tzinfo=tz, fold=1).timestamp())
    if t0 == t1:
        return t0
    ok0 = datetime.fromtimestamp(t0, tz).replace(tzinfo=None) == naive
    ok1 = datetime.fromtimestamp(t1, tz).replace(tzinfo=None) == naive
    if ok0 and ok1:                                   # fall-back overlap: two real instants
        if anomalies is not None:
            anomalies["ambiguous"] = anomalies.get("ambiguous", 0) + 1
        return min(t0, t1) if role == "start" else max(t0, t1)
    if anomalies is not None:                         # spring-forward gap: no such instant
        anomalies["nonexistent"] = anomalies.get("nonexistent", 0) + 1
    return t0                                         # fold=0: shifted forward by the gap


@dataclass(frozen=True, slots=True)
class SessionWindow:
    start_s: int                  # UTC epoch seconds, inclusive
    end_s: int                    # UTC epoch seconds, exclusive
    trading_date: str             # exchange trading date YYYYMMDD (local date of the last second)

    def contains(self, ts_s: int) -> bool:
        return self.start_s <= ts_s < self.end_s


class CalendarError(ValueError):
    pass


def _parse_hhmm(s: str) -> tuple[int, int]:
    if len(s) != 4 or not s.isdigit():
        raise CalendarError(f"bad time {s!r}")
    hh, mm = int(s[:2]), int(s[2:])
    if hh > 24 or mm > 59 or (hh == 24 and mm):
        raise CalendarError(f"bad time {s!r}")
    return hh, mm


def _parse_date(s: str) -> tuple[int, int, int]:
    if len(s) != 8 or not s.isdigit():
        raise CalendarError(f"bad date {s!r}")
    y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
    datetime(y, mo, d)            # validates
    return y, mo, d


def parse_hours(spec: str, tz: ZoneInfo, anomalies: dict[str, int]) -> tuple[SessionWindow, ...]:
    """Parse an IBKR hours string into sorted, merged UTC windows. Raises CalendarError."""
    raw: list[tuple[int, int]] = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise CalendarError(f"bad entry {part!r}")
        day, rest = part.split(":", 1)
        sy, smo, sd = _parse_date(day)
        if rest.strip().upper() == "CLOSED":
            continue
        for rng in rest.split(","):
            rng = rng.strip()
            if not rng:
                continue
            if "-" not in rng:
                raise CalendarError(f"bad range {rng!r}")
            a, b = rng.split("-", 1)
            sh, sm = _parse_hhmm(a)
            start = local_to_utc_s(sy, smo, sd, sh, sm, tz, "start", anomalies)
            if ":" in b:                                          # current format: explicit end date
                edate, etime = b.split(":", 1)
                ey, emo, ed = _parse_date(edate)
                eh, em = _parse_hhmm(etime)
            else:                                                 # legacy: same day / crosses midnight
                eh, em = _parse_hhmm(b)
                base = datetime(sy, smo, sd)
                if (eh, em) <= (sh, sm):
                    base += timedelta(days=1)
                ey, emo, ed = base.year, base.month, base.day
            end = local_to_utc_s(ey, emo, ed, eh, em, tz, "end", anomalies)
            if end <= start:
                raise CalendarError(f"empty/negative window {part!r}")
            raw.append((start, end))
    raw.sort()
    merged: list[list[int]] = []
    for s, e in raw:                                  # dedupe + merge overlapping/touching windows
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        last_local = datetime.fromtimestamp(e - 1, tz)
        out.append(SessionWindow(s, e, last_local.strftime("%Y%m%d")))
    return tuple(out)


class SessionCalendar:
    """Immutable parsed calendar. ``valid`` is False when the zone or hours cannot be parsed:
    consumers then treat the session as UNKNOWN (no empty bars, no session context)."""

    __slots__ = ("time_zone", "trading", "liquid", "valid", "error", "anomalies", "_tstarts", "_tends", "_lstarts")

    def __init__(self, time_zone: str, trading_hours: str, liquid_hours: str) -> None:
        self.time_zone = time_zone
        self.anomalies: dict[str, int] = {}
        self.trading: tuple[SessionWindow, ...] = ()
        self.liquid: tuple[SessionWindow, ...] = ()
        self.valid = False
        self.error = ""
        try:
            if not trading_hours.strip():
                raise CalendarError("no tradingHours")
            tz = load_zone(time_zone)
            self.trading = parse_hours(trading_hours, tz, self.anomalies)
            self.liquid = parse_hours(liquid_hours, tz, self.anomalies) if liquid_hours.strip() else ()
            if not self.trading:
                raise CalendarError("tradingHours has no open window")
            self.valid = True
        except (CalendarError, ZoneInfoNotFoundError, ValueError) as exc:
            self.trading, self.liquid = (), ()
            self.error = f"{type(exc).__name__}: {exc}"
        self._tstarts = [w.start_s for w in self.trading]
        self._tends = [w.end_s for w in self.trading]
        self._lstarts = [w.start_s for w in self.liquid]

    @staticmethod
    def _find(windows: tuple[SessionWindow, ...], starts: list[int], ts_s: int) -> SessionWindow | None:
        i = bisect.bisect_right(starts, ts_s) - 1
        if i >= 0 and windows[i].contains(ts_s):
            return windows[i]
        return None

    def trading_at(self, ts_s: int) -> SessionWindow | None:
        return self._find(self.trading, self._tstarts, ts_s)

    def liquid_at(self, ts_s: int) -> SessionWindow | None:
        return self._find(self.liquid, self._lstarts, ts_s)

    def liquid_in(self, w: SessionWindow) -> SessionWindow | None:
        """First RTH window inside trading window ``w`` (None if the session has no RTH)."""
        i = bisect.bisect_left(self._lstarts, w.start_s)
        if i < len(self.liquid) and self.liquid[i].start_s < w.end_s:
            return self.liquid[i]
        return None

    def next_trading_start(self, ts_s: int) -> int | None:
        i = bisect.bisect_right(self._tstarts, ts_s)
        return self.trading[i].start_s if i < len(self.trading) else None

    def touches_boundary(self, start_s: int, end_s: int) -> bool:
        """Does a trading-session open fall in [start, end) or a close in (start, end]?"""
        i = bisect.bisect_left(self._tstarts, start_s)
        if i < len(self.trading) and self.trading[i].start_s < end_s:
            return True
        j = bisect.bisect_right(self._tends, start_s)
        return j < len(self.trading) and self._tends[j] <= end_s

    def trading_date_for(self, start_s: int, end_s: int) -> str:
        w = self.trading_at(start_s) or self.trading_at(end_s - 1)
        return w.trading_date if w is not None else ""


# ---------------------------------------------------------------------------- statistics

@dataclass(frozen=True, slots=True)
class SessionStats:
    open: int | None
    high: int | None
    low: int | None
    last: int | None
    volume: int
    trades: int
    vwap_num: int                 # sum(price_units * size): exact integer accumulator

    @property
    def vwap(self) -> float | None:
        return self.vwap_num / self.volume if self.volume else None


@dataclass(slots=True)
class _Stats:
    open: int | None = None
    high: int | None = None
    low: int | None = None
    last: int | None = None
    volume: int = 0
    trades: int = 0
    vwap_num: int = 0

    def add(self, price: int, size: int) -> None:
        if self.open is None:
            self.open = self.high = self.low = price
        else:
            if price > self.high:  # type: ignore[operator]
                self.high = price
            if price < self.low:  # type: ignore[operator]
                self.low = price
        self.last = price
        self.volume += size
        self.trades += 1
        self.vwap_num += price * size

    def frozen(self) -> SessionStats:
        return SessionStats(self.open, self.high, self.low, self.last, self.volume, self.trades, self.vwap_num)


@dataclass(frozen=True, slots=True)
class PreviousSession:
    """Only ever built from a session that was actually observed (never fabricated)."""
    trading_date: str
    start_s: int
    end_s: int
    high: int
    low: int
    close: int
    volume: int
    vwap_num: int
    observed_from_open: bool
    gap_observed: bool

    @property
    def vwap(self) -> float | None:
        return self.vwap_num / self.volume if self.volume else None


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    calendar_ok: bool
    calendar_error: str
    time_zone: str
    in_trading_session: bool
    in_rth: bool
    trading_date: str
    session_start_s: int | None
    session_end_s: int | None
    rth_start_s: int | None
    rth_end_s: int | None
    session: SessionStats | None
    rth: SessionStats | None
    overnight: SessionStats | None        # pre-RTH part of the trading session (None: undefinable)
    observed_from_open: bool
    gap_observed: bool                    # a data gap/interruption hit this session: stats may be incomplete
    previous: PreviousSession | None
    trades_outside_session: int
    late_session_trades: int
    calendar_anomalies: tuple[tuple[str, int], ...]


@dataclass(slots=True)
class _Ctx:
    window: SessionWindow
    rth_window: SessionWindow | None
    observed_from_open: bool
    gap_observed: bool
    stats: _Stats = field(default_factory=_Stats)
    rth: _Stats = field(default_factory=_Stats)
    overnight: _Stats = field(default_factory=_Stats)


class SessionTracker:
    __slots__ = ("calendar", "grace_s", "current", "previous", "observing_since_s", "gap_active",
                 "trades_outside_session", "late_session_trades", "in_session", "in_rth", "_valid_until_s",
                 "_ver", "_snap")

    def __init__(self, close_grace_ms: int = 500) -> None:
        self.calendar: SessionCalendar | None = None
        self.grace_s = close_grace_ms / 1000.0
        self.current: _Ctx | None = None
        self.previous: PreviousSession | None = None
        self.observing_since_s: float | None = None
        self.gap_active = False
        self.trades_outside_session = 0
        self.late_session_trades = 0
        self.in_session = False
        self.in_rth = False
        self._valid_until_s: float = -1.0
        self._ver = 0
        self._snap: tuple[int, SessionSnapshot] | None = None

    @property
    def ok(self) -> bool:
        return self.calendar is not None and self.calendar.valid

    def set_calendar(self, cal: SessionCalendar) -> None:
        self.calendar = cal
        self._valid_until_s = -1.0
        self._ver += 1

    # ------------------------------------------------------------------ gaps
    def set_gap(self, active: bool) -> None:
        self.gap_active = active
        self._ver += 1
        if active and self.current is not None:
            self.current.gap_observed = True

    def mark_gap(self) -> None:
        self._ver += 1
        if self.current is not None:
            self.current.gap_observed = True

    # ------------------------------------------------------------------ transitions
    def _open(self, w: SessionWindow) -> None:
        self._close_current()
        since = self.observing_since_s
        self.current = _Ctx(w, self.calendar.liquid_in(w),  # type: ignore[union-attr]
                            observed_from_open=since is not None and since <= w.start_s,
                            gap_observed=self.gap_active)

    def _close_current(self) -> None:
        cur = self.current
        if cur is None:
            return
        s = cur.stats
        self.previous = (PreviousSession(cur.window.trading_date, cur.window.start_s, cur.window.end_s,
                                         s.high, s.low, s.last, s.volume, s.vwap_num,  # type: ignore[arg-type]
                                         cur.observed_from_open, cur.gap_observed)
                         if s.trades else None)
        self.current = None

    def advance(self, wm_ns: int) -> None:
        """Event-time watermark (ns). Opens/closes session contexts at calendar boundaries."""
        now = wm_ns / 1e9
        if self.observing_since_s is None:
            self.observing_since_s = now
        if now < self._valid_until_s or not self.ok:
            return
        cal = self.calendar
        cur = self.current
        ts = int(now)
        self._ver += 1
        if cur is not None and now >= cur.window.end_s + self.grace_s:
            self._close_current()
            cur = None
        w = cal.trading_at(ts)  # type: ignore[union-attr]
        if w is not None and (cur is None or w.start_s > cur.window.start_s):
            self._open(w)
        self.in_session = w is not None
        r = cal.liquid_at(ts)  # type: ignore[union-attr]
        self.in_rth = r is not None
        # next instant at which anything above can change
        cands = []
        if self.current is not None:
            cands.append(self.current.window.end_s + self.grace_s)
        nxt = cal.next_trading_start(ts)  # type: ignore[union-attr]
        if nxt is not None:
            cands.append(nxt)
        if w is not None:
            cands.append(w.end_s)
        if r is not None:
            cands.append(r.end_s)
        else:
            i = bisect.bisect_right(cal._lstarts, ts)  # type: ignore[union-attr]
            if i < len(cal.liquid):  # type: ignore[union-attr]
                cands.append(cal.liquid[i].start_s)  # type: ignore[union-attr]
        self._valid_until_s = min(cands) if cands else float("inf")

    def on_trade(self, ts_s: int, price: int, size: int) -> None:
        """A bar-eligible, non-late print with exchange timestamp ``ts_s``."""
        if not self.ok:
            return
        self._ver += 1
        cur = self.current
        if cur is None or not cur.window.contains(ts_s):
            w = self.calendar.trading_at(ts_s)  # type: ignore[union-attr]
            if w is None:
                self.trades_outside_session += 1
                return
            if cur is not None and w.start_s < cur.window.start_s:
                self.late_session_trades += 1          # never rewrites a finished session
                return
            if cur is None and self.previous is not None and w.start_s <= self.previous.start_s:
                self.late_session_trades += 1
                return
            self._open(w)
            cur = self.current
        cur.stats.add(price, size)  # type: ignore[union-attr]
        rw = cur.rth_window  # type: ignore[union-attr]
        if rw is not None:
            if rw.contains(ts_s):
                cur.rth.add(price, size)  # type: ignore[union-attr]
            elif ts_s < rw.start_s:
                cur.overnight.add(price, size)  # type: ignore[union-attr]

    # ------------------------------------------------------------------ snapshot
    def token(self) -> tuple:
        cur = self.current
        return (cur.window.start_s if cur else None, self.in_session, self.in_rth, self.previous is not None,
                cur.gap_observed if cur else None)

    def snapshot(self) -> SessionSnapshot:
        c = self._snap
        if c is not None and c[0] == self._ver:
            return c[1]
        cal = self.calendar
        cur = self.current
        rw = cur.rth_window if cur else None
        snap = SessionSnapshot(
            calendar_ok=self.ok, calendar_error=cal.error if cal else "no contract details",
            time_zone=cal.time_zone if cal else "",
            in_trading_session=self.in_session, in_rth=self.in_rth,
            trading_date=cur.window.trading_date if cur else "",
            session_start_s=cur.window.start_s if cur else None, session_end_s=cur.window.end_s if cur else None,
            rth_start_s=rw.start_s if rw else None, rth_end_s=rw.end_s if rw else None,
            session=cur.stats.frozen() if cur else None,
            rth=cur.rth.frozen() if cur and rw else None,
            overnight=cur.overnight.frozen() if cur and rw else None,
            observed_from_open=cur.observed_from_open if cur else False,
            gap_observed=cur.gap_observed if cur else False,
            previous=self.previous, trades_outside_session=self.trades_outside_session,
            late_session_trades=self.late_session_trades,
            calendar_anomalies=tuple(sorted(cal.anomalies.items())) if cal else ())
        self._snap = (self._ver, snap)
        return snap
