"""C9a — deterministic DecisionContext: the evidence Hermès actually has at one event-state.

``build_decision_context(snapshot, cfg)`` is a PURE function of an immutable ``MarketSnapshot``
(plus the ``[decision]`` config). It reads no clock, keeps no state, performs no I/O and has no
order path. Same raw recording + same config + same code => same snapshot => same context.

It exposes evidence only. There is no score, no LONG/SHORT label and no strategy threshold here;
setup candidates (C9b) consume this object.

Evidence rules
--------------
* Every section carries ``available`` + ``reason``. Unavailable evidence is ``None`` — never a
  fabricated zero or a stale value presented as current.
* Prices are integer grid units (MNQ: 1 unit = 1 tick = 0.25 pt); ratios are kept as exact integer
  numerator/denominator pairs with float convenience properties only.
* UNKNOWN aggressor volume stays separate everywhere; it is never redistributed to BUY/SELL.
* IBKR CME depth is aggregated MBP, not MBO: C8 fields are visible-liquidity measurements and
  "compatible" upper bounds — never proof of absorption, icebergs, spoofing, cancellations,
  individual orders or queue position (``EPISTEMIC_NOTES`` travels with every context).
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.config import DecisionConfig
from hermes.market.absorption import AbsorptionSnapshot
from hermes.market.bars import Bar, BarFlag, BarsSnapshot
from hermes.market.metrics import MetricsSnapshot
from hermes.market.patterns import PatternSnapshot
from hermes.market.sessions import SessionSnapshot
from hermes.market.snapshot import InstrumentSnapshot, MarketSnapshot
from hermes.market.structure import StructureSnapshot

CONTEXT_SCHEMA_VERSION = 4

EPISTEMIC_NOTES = (
    "IBKR CME depth is aggregated market-by-price (MBP), not market-by-order (MBO)",
    "a visible size decrease is liquidity removed from view, not necessarily a cancellation",
    "replenishment/absorption fields are compatible measurements and aggregate upper bounds, not proof",
    "icebergs, spoofing, individual order identity and queue position are unknowable from this feed",
    "UNKNOWN aggressor volume is reported separately and never redistributed to BUY/SELL",
    "walls/visible liquidity can disappear; depth is confirmation evidence, never a standalone signal",
)

_QUALITY_BAD = BarFlag.DATA_GAP | BarFlag.MARKET_DATA_INVALID | BarFlag.CONNECTION_INTERRUPTION


# ---------------------------------------------------------------------------- sections

@dataclass(frozen=True, slots=True)
class DataQuality:
    """Health facts copied from the snapshot; C9d gates evaluate these (C9a only reports)."""
    connection: str
    farm_broken: bool
    not_live: bool
    conflict_phase: str
    alerts: tuple[str, ...]
    contract_state: str
    market_data_type: int | None
    market_data_ok: bool
    not_ok_reasons: tuple[str, ...]
    book_state: str | None
    book_issues: tuple[str, ...]
    book_epoch: int | None
    streams: tuple[tuple[str, int | None, str], ...]      # (stream, generation, effective status)
    tape_epoch: int | None
    classifier_epoch: int | None
    classification_context_ok: bool
    classification_context_reason: str
    metrics_epoch: int | None
    structure_epoch: int | None
    pattern_epoch: int | None
    bar_active_flags: int                                # BarFlag of the quality condition active NOW
    session_calendar_ok: bool
    required_streams: tuple[str, ...] = ()
    book_coherent: bool = False                          # snapshot usable as a price observation
    book_coherence_reason: str = "no_book"


@dataclass(frozen=True, slots=True)
class PriceContext:
    available: bool
    reason: str
    mid_x2: int | None                 # best bid + best ask (exact; mid = mid_x2 / 2) — VALID book only
    best_bid: int | None
    best_ask: int | None
    spread_units: int | None
    bbo_bid: int | None                # tick-by-tick BidAsk (primary BBO)
    bbo_ask: int | None
    last_trade_price: int | None
    last_trade_size: int | None
    micro_num: int | None              # microprice = micro_num / micro_den (units)
    micro_den: int | None
    units_per_point: int | None = None # exact grid facts (None: grid not uniform / unknown)
    tick_units: int | None = None

    @property
    def mid_units(self) -> float | None:
        return None if self.mid_x2 is None else self.mid_x2 / 2

    @property
    def microprice_units(self) -> float | None:
        return None if self.micro_num is None or not self.micro_den else self.micro_num / self.micro_den


@dataclass(frozen=True, slots=True)
class BarContext:
    """Descriptive, label-free summary of the last ``lookback`` COMPLETED bars of one timeframe."""
    timeframe_s: int
    available: bool
    reason: str                        # ok | partial_lookback:k/n | no_completed_bars | no_traded_bars | bars_disabled
    lookback: int
    bars_used: int                     # completed bars actually available (<= lookback)
    traded_bars: int                   # of those, bars with bar-eligible prints
    empty_bars: int
    latest: Bar | None                 # most recent COMPLETED bar
    forming: Bar | None                # current forming bar (never final; FORMING flag set)
    open: int | None                   # first traded bar open in the lookback
    close: int | None                  # latest traded bar close
    high: int | None
    low: int | None
    net_change_units: int | None       # close - open over the lookback (traded bars only)
    up_bars: int                       # close > open
    down_bars: int                     # close < open
    flat_bars: int                     # close == open (traded)
    volume: int
    buy_volume: int
    sell_volume: int
    unknown_volume: int                # never redistributed
    known_delta: int                   # buy - sell (UNKNOWN excluded)
    vwap_num: int                      # sum(price * size) over the lookback
    flags: int                         # BarFlag OR over lookback bars (+ forming)
    quality_ok: bool                   # no DATA_GAP / MARKET_DATA_INVALID / CONNECTION_INTERRUPTION in lookback

    @property
    def vwap_units(self) -> float | None:
        return self.vwap_num / self.volume if self.volume else None

    @property
    def range_units(self) -> int | None:
        return None if self.high is None or self.low is None else self.high - self.low



@dataclass(frozen=True, slots=True)
class SessionContext:
    available: bool
    reason: str
    session: SessionSnapshot | None
    in_trading_session: bool
    in_rth: bool
    # price relative to the FULL trading-session VWAP (incl. overnight): (mid - vwap) = num / den units (exact)
    vs_vwap_num: int | None
    vs_vwap_den: int | None
    # price relative to the RTH VWAP (primary reference for RTH entries); None until RTH prints exist
    vs_rth_vwap_num: int | None = None
    vs_rth_vwap_den: int | None = None

    @property
    def vs_vwap_units(self) -> float | None:
        return None if self.vs_vwap_num is None or not self.vs_vwap_den else self.vs_vwap_num / self.vs_vwap_den

    @property
    def vs_rth_vwap_units(self) -> float | None:
        return (None if self.vs_rth_vwap_num is None or not self.vs_rth_vwap_den
                else self.vs_rth_vwap_num / self.vs_rth_vwap_den)


@dataclass(frozen=True, slots=True)
class Section:
    """Availability wrapper around an already availability-aware C7/C8 snapshot."""
    available: bool
    reason: str
    epoch: int | None


@dataclass(frozen=True, slots=True)
class DecisionContext:
    schema_version: int
    instrument_id: int
    symbol: str
    seq: int                           # last raw seq reflected (deterministic event position)
    mono_ns: int                       # recorded event time of that state
    wall_ns: int
    quality: DataQuality
    price: PriceContext
    bars_5m: BarContext                # regime / market context
    bars_1m: BarContext                # setup confirmation / local structure
    bars_30s: BarContext               # execution timing
    regime_5m: BarContext              # C9b: last [decision].regime_bars_5m completed 5 m bars
    setup_1m: BarContext               # C9b: last [decision].setup_bars_1m completed 1 m bars
    recent_1m_window: BarContext       # C9b: last [decision].recent_window_bars_1m completed 1 m bars; its
                                       # high/low are the recent_1m_window_high/_low (rolling extreme, NOT a pivot)
    session: SessionContext
    flow: Section                      # C7
    metrics: MetricsSnapshot | None
    structure_section: Section         # C8 visible-liquidity structure
    structure: StructureSnapshot | None
    patterns_section: Section          # C8 sweeps / follow-through
    patterns: PatternSnapshot | None
    absorption_section: Section        # C8 absorption-COMPATIBLE context (never proof)
    absorption: AbsorptionSnapshot | None
    notes: tuple[str, ...] = EPISTEMIC_NOTES


# ---------------------------------------------------------------------------- builders

def _quality(snap: MarketSnapshot, i: InstrumentSnapshot) -> DataQuality:
    from hermes.decision.safety import EMPTY_SIDE_TRANSITION, rows_coherence
    b, t = i.book, i.tape
    coh = rows_coherence(b.bids, b.asks) if b is not None else (False, EMPTY_SIDE_TRANSITION)
    return DataQuality(
        connection=snap.connection.value, farm_broken=snap.farm_broken, not_live=snap.not_live,
        conflict_phase=snap.conflict_phase, alerts=tuple(snap.alerts), contract_state=i.contract_state,
        market_data_type=i.market_data_type, market_data_ok=i.market_data_ok, not_ok_reasons=tuple(i.not_ok_reasons),
        book_state=b.state.value if b else None, book_issues=tuple(sorted(x.value for x in b.issues)) if b else (),
        book_epoch=b.epoch if b else None,
        streams=tuple((s.stream.value, s.generation, s.status.value) for s in i.streams),
        tape_epoch=t.epoch if t else None, classifier_epoch=t.classifier_epoch if t else None,
        classification_context_ok=bool(t and t.context_ok),
        classification_context_reason=t.context_reason if t else "no_tape",
        metrics_epoch=i.metrics.continuity_epoch if i.metrics else None,
        structure_epoch=i.structure.continuity_epoch if i.structure else None,
        pattern_epoch=i.patterns.continuity_epoch if i.patterns else None,
        bar_active_flags=int(i.bars.active_flags) if i.bars else 0,
        session_calendar_ok=bool(i.session and i.session.calendar_ok),
        required_streams=tuple(x.value for x in i.required_streams),
        book_coherent=coh[0], book_coherence_reason=coh[1],
    )


def _price(i: InstrumentSnapshot) -> PriceContext:
    b = i.book
    valid = b is not None and b.is_valid and bool(b.bids) and bool(b.asks)
    bm = i.metrics.book if i.metrics is not None else None
    lt = i.last_trade
    bbo = i.bbo
    if valid:
        bid, ask = b.bids[0][0], b.asks[0][0]  # type: ignore[union-attr]
        reason = "ok"
    else:
        bid = ask = None
        reason = f"book_{b.state.value}" if b is not None else "no_book"
    return PriceContext(
        available=valid, reason=reason,
        mid_x2=(bid + ask) if valid else None, best_bid=bid, best_ask=ask,  # type: ignore[operator]
        spread_units=(ask - bid) if valid else None,  # type: ignore[operator]
        bbo_bid=bbo.bid_units if bbo else None, bbo_ask=bbo.ask_units if bbo else None,
        last_trade_price=lt.price_units if lt else None, last_trade_size=lt.size if lt else None,
        micro_num=bm.micro_num if valid and bm is not None and bm.available else None,
        micro_den=bm.micro_den if valid and bm is not None and bm.available else None,
        units_per_point=i.units_per_point, tick_units=i.tick_units,
    )


def bar_context(bars: BarsSnapshot | None, tf: int, lookback: int) -> BarContext:
    if bars is None:
        return _empty_bar_context(tf, lookback, "bars_disabled", None)
    latest_all = {30: bars.latest_30s, 60: bars.latest_1m, 300: bars.latest_5m}[tf]
    forming = {30: bars.forming_30s, 60: bars.forming_1m, 300: bars.forming_5m}[tf]
    window = tuple(reversed(latest_all[:lookback]))           # oldest -> newest
    if not window:
        return _empty_bar_context(tf, lookback, "no_completed_bars", forming)
    traded = [x for x in window if x.trades > 0]
    flags = 0
    for x in window:
        flags |= int(x.flags)
    if forming is not None:
        flags |= int(forming.flags) & ~int(BarFlag.FORMING)
    up = sum(1 for x in traded if x.close > x.open)
    down = sum(1 for x in traded if x.close < x.open)
    buy = sum(x.buy_volume for x in window)
    sell = sum(x.sell_volume for x in window)
    quality_ok = not any(int(x.flags) & int(_QUALITY_BAD) for x in window)
    if not traded:
        reason = "no_traded_bars"
    elif len(window) < lookback:
        reason = f"partial_lookback:{len(window)}/{lookback}"
    else:
        reason = "ok"
    return BarContext(
        timeframe_s=tf, available=bool(traded), reason=reason, lookback=lookback, bars_used=len(window),
        traded_bars=len(traded), empty_bars=len(window) - len(traded), latest=window[-1], forming=forming,
        open=traded[0].open if traded else None, close=traded[-1].close if traded else None,
        high=max(x.high for x in traded) if traded else None, low=min(x.low for x in traded) if traded else None,
        net_change_units=(traded[-1].close - traded[0].open) if traded else None,
        up_bars=up, down_bars=down, flat_bars=len(traded) - up - down,
        volume=sum(x.volume for x in window), buy_volume=buy, sell_volume=sell,
        unknown_volume=sum(x.unknown_volume for x in window), known_delta=buy - sell,
        vwap_num=sum(x.vwap_num for x in window), flags=flags, quality_ok=quality_ok,
    )


def _empty_bar_context(tf: int, lookback: int, reason: str, forming: Bar | None) -> BarContext:
    return BarContext(tf, False, reason, lookback, 0, 0, 0, None, forming, None, None, None, None, None,
                      0, 0, 0, 0, 0, 0, 0, 0, 0, int(forming.flags) & ~int(BarFlag.FORMING) if forming else 0, True)


def _session(i: InstrumentSnapshot, price: PriceContext) -> SessionContext:
    s = i.session
    if s is None:
        return SessionContext(False, "no_session_tracker", None, False, False, None, None)
    if not s.calendar_ok:
        return SessionContext(False, f"calendar_unknown:{s.calendar_error}", s, False, False, None, None)
    num = den = rnum = rden = None
    st, rth = s.session, s.rth
    if st is not None and st.volume and price.mid_x2 is not None:
        num = price.mid_x2 * st.volume - 2 * st.vwap_num
        den = 2 * st.volume
    if rth is not None and rth.volume and price.mid_x2 is not None:
        rnum = price.mid_x2 * rth.volume - 2 * rth.vwap_num
        rden = 2 * rth.volume
    reason = "ok" if s.in_trading_session else "outside_trading_session"
    return SessionContext(True, reason, s, s.in_trading_session, s.in_rth, num, den, rnum, rden)


def _section(snap_obj, attr_available: str = "available", attr_reason: str = "reason",
             epoch: int | None = None) -> Section:
    if snap_obj is None:
        return Section(False, "not_present", epoch)
    return Section(bool(getattr(snap_obj, attr_available)), str(getattr(snap_obj, attr_reason)), epoch)


def build_decision_context(snap: MarketSnapshot, cfg: DecisionConfig | None = None,
                           instrument_id: int | None = None) -> DecisionContext | None:
    """Evidence for one instrument at the snapshot's event-state (None if the instrument is absent)."""
    cfg = cfg or DecisionConfig()
    if not snap.instruments:
        return None
    i = snap.instruments[0] if instrument_id is None else snap.instrument(instrument_id)
    if i is None:
        return None
    price = _price(i)
    m = i.metrics
    flow = (Section(False, "not_present", None) if m is None
            else Section(m.book.available, m.book.reason, m.continuity_epoch))
    st, pt, ab = i.structure, i.patterns, i.absorption
    return DecisionContext(
        schema_version=CONTEXT_SCHEMA_VERSION, instrument_id=i.instrument_id, symbol=i.local_symbol,
        seq=snap.seq, mono_ns=snap.mono_ns, wall_ns=snap.wall_ns,
        quality=_quality(snap, i), price=price,
        bars_5m=bar_context(i.bars, 300, cfg.lookback_5m),
        bars_1m=bar_context(i.bars, 60, cfg.lookback_1m),
        bars_30s=bar_context(i.bars, 30, cfg.lookback_30s),
        regime_5m=bar_context(i.bars, 300, cfg.regime_bars_5m),
        setup_1m=bar_context(i.bars, 60, cfg.setup_bars_1m),
        recent_1m_window=bar_context(i.bars, 60, cfg.recent_window_bars_1m),
        session=_session(i, price),
        flow=flow, metrics=m,
        structure_section=_section(st, epoch=st.continuity_epoch if st else None), structure=st,
        patterns_section=_section(pt, epoch=pt.continuity_epoch if pt else None), patterns=pt,
        absorption_section=_section(ab, epoch=None), absorption=ab,
    )


def context_digest(ctx: DecisionContext) -> str:
    """Canonical SHA-256 of a DecisionContext (sorted, enum-valued; deterministic across runs)."""
    from hermes.replay.fingerprint import digest
    return digest(ctx)
