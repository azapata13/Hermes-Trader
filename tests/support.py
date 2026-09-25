"""Test support: scripted raw IBKR sessions (no TWS) and a normalizer+engine harness."""

from __future__ import annotations

from decimal import Decimal

from hermes.config import BarsConfig, BookConfig, SessionConfig, SubscriptionsConfig, TapeConfig
from hermes.ibkr import raw_events as R
from hermes.ibkr.contracts import ContractSpec
from hermes.ibkr.normalizer import Normalizer
from hermes.market.engine import MarketEngine

MS = 1_000_000
SPEC = ContractSpec("MNQ", "FUT", "CME", "USD", "MNQ", "202612")
DEPTH, BBO, TRADES, L1, CONTRACT = 10_001, 10_002, 10_003, 10_004, 10_000
BASE = 21000.0          # 84000 units
TICK = 0.25


class RawScript:
    """Builds a deterministic sequence of raw events with increasing seq and time."""

    def __init__(self, mono0: int = 10_000 * MS, wall0: int = 1_790_000_000 * 10**9) -> None:
        self.seq = 0
        self.mono = mono0
        self.wall = wall0
        self.events: list[R.RawEvent] = []

    def advance(self, ms: float) -> "RawScript":
        ns = int(ms * MS)
        self.mono += ns
        self.wall += ns
        return self

    def at(self, t_s: float) -> "RawScript":
        """Move wall (and mono) time forward to absolute UTC epoch seconds ``t_s``."""
        ns = round(t_s * 10**9) - self.wall
        assert ns >= 0, "time only moves forward"
        self.mono += ns
        self.wall += ns
        return self

    def add(self, cls, **kw) -> R.RawEvent:
        self.seq += 1
        ev = cls(seq=self.seq, recv_mono_ns=self.mono, recv_wall_ns=self.wall, **kw)
        self.events.append(ev)
        return ev

    # ---- session / control ----
    def next_valid_id(self):
        return self.add(R.RawNextValidId, order_id=1)

    def closed(self):
        return self.add(R.RawConnectionClosed)

    def control(self, kind: str, detail: str = ""):
        return self.add(R.RawControl, kind=kind, detail=detail)

    def tick(self):
        return self.add(R.RawTimerTick, due_mono_ns=self.mono, coalesced=1)

    def heartbeat(self):
        return self.add(R.RawCurrentTime, time=self.wall // 10**9)

    def request(self, method: str, req_id: int | None, iid: int = 1, **params):
        return self.add(R.RawRequestIssued, method=method, req_id=req_id, instrument_id=iid,
                        sent_mono_ns=self.mono, sent_wall_ns=self.wall,
                        params=tuple((k, str(v)) for k, v in params.items()))

    def error(self, req_id: int, code: int, message: str = ""):
        return self.add(R.RawError, req_id=req_id, error_time=0, code=code, message=message)

    # ---- contract ----
    def contract_details(self, req_id: int = CONTRACT, **over):
        base = dict(req_id=req_id, con_id=770561201, symbol="MNQ", sec_type="FUT", local_symbol="MNQZ6",
                    trading_class="MNQ", last_trade_date_or_contract_month="20261218", exchange="CME",
                    primary_exchange="", currency="USD", multiplier="2", min_tick=0.25,
                    market_rule_ids="67,67", valid_exchanges="CME,QBALGO", time_zone_id="US/Central",
                    trading_hours="", liquid_hours="")
        base.update(over)
        return self.add(R.RawContractDetails, **base)

    def contract_end(self, req_id: int = CONTRACT):
        return self.add(R.RawContractDetailsEnd, req_id=req_id)

    def market_rule(self, rule_id: int = 67, increments=((0.0, 0.25),)):
        return self.add(R.RawMarketRule, market_rule_id=rule_id, increments=increments)

    # ---- market data ----
    def depth(self, req_id: int, position: int, operation: int, side: int, price: float, size, l2=False):
        return self.add(R.RawMarketDepth, req_id=req_id, position=position, operation=operation, side=side,
                        price=price, size=Decimal(str(size)), is_l2=l2)

    def bbo(self, req_id: int, bid: float, ask: float, bid_size=5, ask_size=5):
        return self.add(R.RawTickByTickBidAsk, req_id=req_id, time=self.wall // 10**9, bid_price=bid,
                        ask_price=ask, bid_size=Decimal(bid_size), ask_size=Decimal(ask_size),
                        bid_past_low=False, ask_past_high=False)

    def trade(self, req_id: int, price: float, size=1, past_limit=False, unreported=False, special=""):
        return self.add(R.RawTickByTickAllLast, req_id=req_id, tick_type=2, time=self.wall // 10**9,
                        price=price, size=Decimal(size), past_limit=past_limit, unreported=unreported,
                        exchange="CME", special_conditions=special)

    def mdt(self, req_id: int = L1, market_data_type: int = 1):
        return self.add(R.RawMarketDataType, req_id=req_id, market_data_type=market_data_type)

    def tick_price(self, req_id: int, tick_type: int, price: float):
        return self.add(R.RawTickPrice, req_id=req_id, tick_type=tick_type, price=price)

    # ---- composite scenarios ----
    def bootstrap(self, depth=DEPTH, bbo=BBO, trades=TRADES, l1=L1, **contract):
        """Connect, resolve contract, define the grid, subscribe all four streams.
        ``contract`` overrides contract-details fields (e.g. trading_hours / liquid_hours)."""
        self.next_valid_id()
        self.request("reqMarketDataType", None, iid=0, market_data_type=1)
        self.request("reqContractDetails", CONTRACT, **dict(SPEC.to_params()))
        self.contract_details(**contract)
        self.contract_end()
        self.request("reqMarketRule", None, rule_id=67)
        self.market_rule()
        self.subscribe(depth, bbo, trades, l1)
        return self

    def subscribe(self, depth=DEPTH, bbo=BBO, trades=TRADES, l1=L1):
        if depth:
            self.request("reqMktDepth", depth, num_rows=10)
        if bbo:
            self.request("reqTickByTickData", bbo, tick_type="BidAsk")
        if trades:
            self.request("reqTickByTickData", trades, tick_type="AllLast")
        if l1:
            self.request("reqMktData", l1)
        return self

    def seed_book(self, depth=DEPTH, bbo=BBO, l1=L1, rows=5, mid_bid=BASE):
        """Populate `rows` levels per side, send a matching BBO and LIVE marketDataType."""
        for i in range(rows):
            self.depth(depth, i, 0, 1, mid_bid - TICK * i, 10 + i)            # bids
            self.depth(depth, i, 0, 0, mid_bid + TICK * (i + 1), 10 + i)      # asks
        self.bbo(bbo, mid_bid, mid_bid + TICK)
        if l1:
            self.mdt(l1, 1)
        return self


def book_cfg(**kw) -> BookConfig:
    return BookConfig(**kw)


def write_hrec(session_dir, events, meta: dict | None = None, final: bool = True, gaps=(),
               rotate_at: int | None = None, session_id: str = "test-session", header_over: dict | None = None):
    """Write raw events as a .hrec session synchronously (deterministic test recordings).

    ``gaps``: iterable of (index, first_seq, last_seq, reason) -> a GAP record before events[index].
    ``rotate_at``: start part-0002 before events[rotate_at]. Returns the session directory.
    """
    import dataclasses as _dc
    from pathlib import Path as _P

    from hermes.config import HermesConfig
    from hermes.ibkr.normalizer import NORMALIZER_VERSION
    from hermes.storage.codec import MAGIC, Encoder

    d = _P(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    m = {"hermes_version": "test", "normalizer_version": NORMALIZER_VERSION, "ibapi_version": "10.45.1",
         "python": "3.11", "contract_spec": dict(SPEC.to_params()), "config": _dc.asdict(HermesConfig())}
    m.update(meta or {})
    enc = Encoder()
    gap_at = {g[0]: g[1:] for g in gaps}
    part, fh = 0, None

    def open_part():
        nonlocal part, fh
        part += 1
        fh = open(d / f"part-{part:04d}.hrec", "wb")
        hdr = {"session_id": session_id, "part": part, "created_wall_ns": 0, "seq_origin": 1, "meta": m}
        hdr.update(header_over or {})
        fh.write(MAGIC + enc.header(hdr))

    open_part()
    for i, ev in enumerate(events):
        if rotate_at is not None and i == rotate_at:
            fh.write(enc.footer({"final": False}))
            fh.close()
            open_part()
        if i in gap_at:
            a, b, why = gap_at[i]
            fh.write(enc.gap(a, b, b - a + 1, why, 0, 0))
        fh.write(enc.raw(ev))
    if final:
        fh.write(enc.footer({"final": True}))
    fh.close()
    return d


class Harness:
    """Normalizer + MarketEngine driven by raw events (what the live pipeline and replay do)."""

    def __init__(self, book: BookConfig | None = None, session: SessionConfig | None = None,
                 subs: SubscriptionsConfig | None = None, tape: TapeConfig | None = None,
                 bars: BarsConfig | None = None) -> None:
        self.normalizer = Normalizer()
        self.engine = MarketEngine(book or BookConfig(), session or SessionConfig(), subs or SubscriptionsConfig(),
                                   tape_cfg=tape, bars_cfg=bars)
        self.market_events = []

    def feed(self, raws) -> "Harness":
        for raw in raws:
            for ev in self.normalizer.normalize(raw):
                self.market_events.append(ev)
                self.engine.on_event(ev)
        return self

    def run(self, script: RawScript, start: int = 0) -> "Harness":
        return self.feed(script.events[start:])
