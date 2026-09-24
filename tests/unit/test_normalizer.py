from __future__ import annotations

from decimal import Decimal

from hermes.ibkr.normalizer import Normalizer
from hermes.market import events as M
from hermes.market.events import AnomalyKind, BookSide, DepthOp, ErrorClass, Stream
from tests.support import BBO, CONTRACT, DEPTH, L1, TRADES, RawScript


def boot() -> tuple[Normalizer, RawScript]:
    s = RawScript().bootstrap()
    n = Normalizer()
    for raw in s.events:
        n.normalize(raw)
    return n, s


def norm(n: Normalizer, raw) -> tuple:
    return n.normalize(raw)


def test_bootstrap_defines_instrument_and_generations():
    s = RawScript().bootstrap()
    n = Normalizer()
    out = [ev for raw in s.events for ev in n.normalize(raw)]
    kinds = [type(e).__name__ for e in out]
    assert kinds.count("SubscriptionEvent") == 4
    assert "ContractResolvedEvent" in kinds and "InstrumentDefinitionEvent" in kinds
    idef = next(e for e in out if isinstance(e, M.InstrumentDefinitionEvent))
    assert idef.local_symbol == "MNQZ6" and str(idef.price_grid.unit) == "0.25"
    for stream, rid in ((Stream.DEPTH, DEPTH), (Stream.BBO, BBO), (Stream.TRADES, TRADES), (Stream.L1, L1)):
        assert n.active_generation(1, stream) == rid


def test_depth_mapping_ibkr_codes():
    n, s = boot()
    (ev,) = norm(n, s.depth(DEPTH, 0, 0, 1, 21000.25, 7))       # insert BID
    assert isinstance(ev, M.DepthRowEvent)
    assert (ev.side, ev.op, ev.position, ev.price_units, ev.size, ev.generation) == (
        BookSide.BID, DepthOp.INSERT, 0, 84001, 7, DEPTH)
    (ev,) = norm(n, s.depth(DEPTH, 3, 1, 0, 21001.0, 2))        # update ASK
    assert (ev.side, ev.op) == (BookSide.ASK, DepthOp.UPDATE)
    (ev,) = norm(n, s.depth(DEPTH, 2, 2, 0, 0.0, 0))            # delete: price irrelevant
    assert ev.op is DepthOp.DELETE


def test_depth_anomalies():
    n, s = boot()
    for raw, kind in [(s.depth(DEPTH, 0, 0, 1, 21000.1, 1), AnomalyKind.OFF_GRID_PRICE),
                      (s.depth(DEPTH, 0, 7, 1, 21000.0, 1), AnomalyKind.INVALID_CODE),
                      (s.depth(DEPTH, 0, 0, 5, 21000.0, 1), AnomalyKind.INVALID_CODE),
                      (s.depth(DEPTH, 0, 0, 1, 21000.0, "1.5"), AnomalyKind.NON_INTEGRAL_SIZE)]:
        (ev,) = norm(n, raw)
        assert isinstance(ev, M.DataAnomalyEvent) and ev.kind is kind and ev.generation == DEPTH


def test_no_grid_before_market_rule():
    s = RawScript()
    n = Normalizer()
    for raw in [s.request("reqMktDepth", DEPTH), s.depth(DEPTH, 0, 0, 1, 21000.0, 1)]:
        out = n.normalize(raw)
    assert out[0].kind is AnomalyKind.NO_PRICE_GRID


def test_old_generation_callbacks_never_become_market_events():
    """Amendment A: after resubscription, callbacks of the old reqId only produce anomalies."""
    n, s = boot()
    norm(n, s.request("cancelMktDepth", DEPTH))
    norm(n, s.request("reqMktDepth", 20_001))
    assert n.active_generation(1, Stream.DEPTH) == 20_001
    (ev,) = norm(n, s.depth(DEPTH, 0, 0, 1, 21000.0, 1))                 # late old callback
    assert isinstance(ev, M.DataAnomalyEvent) and ev.kind is AnomalyKind.INACTIVE_REQ_ID
    (ev,) = norm(n, s.depth(20_001, 0, 0, 1, 21000.0, 1))
    assert isinstance(ev, M.DepthRowEvent) and ev.generation == 20_001
    # same for BBO / trades / L1 / 317 on an old depth generation
    norm(n, s.request("reqTickByTickData", 20_002, tick_type="BidAsk"))
    (ev,) = norm(n, s.bbo(BBO, 21000.0, 21000.25))
    assert ev.kind is AnomalyKind.INACTIVE_REQ_ID
    out = norm(n, s.error(DEPTH, 317, "Market depth data has been RESET"))
    assert len(out) == 1 and isinstance(out[0], M.ErrorEvent) and not out[0].generation_active


def test_unknown_req_id():
    n, s = boot()
    (ev,) = norm(n, s.trade(99_999, 21000.0))
    assert ev.kind is AnomalyKind.UNKNOWN_REQ_ID


def test_bbo_and_trade():
    n, s = boot()
    (ev,) = norm(n, s.bbo(BBO, 21000.0, 21000.25, 3, 4))
    assert (ev.bid_units, ev.ask_units, ev.bid_size, ev.ask_size) == (84000, 84001, 3, 4)
    (ev,) = norm(n, s.bbo(BBO, 0.0, -1.0))
    assert ev.bid_units is None and ev.ask_units is None
    out = norm(n, s.bbo(BBO, 21000.1, 21000.25))
    assert out[0].bid_units is None and out[1].kind is AnomalyKind.OFF_GRID_PRICE
    (ev,) = norm(n, s.trade(TRADES, 21000.5, 3))
    assert isinstance(ev, M.TradeEvent) and (ev.price_units, ev.size) == (84002, 3)


def test_l1_and_market_data_type():
    n, s = boot()
    (ev,) = norm(n, s.tick_price(L1, 1, 21000.0))
    assert ev.field is M.L1Field.BID and ev.price_units == 84000
    (ev,) = norm(n, s.tick_price(L1, 66, 21000.0))                     # DELAYED_BID
    assert ev.kind is AnomalyKind.DELAYED_TICK
    (ev,) = norm(n, s.mdt(L1, 1))
    assert isinstance(ev, M.MarketDataTypeEvent) and ev.is_live and ev.generation == L1
    (ev,) = norm(n, s.mdt(L1, 3))
    assert not ev.is_live


def test_errors_routed_and_classified():
    n, s = boot()
    out = norm(n, s.error(DEPTH, 317, "Market depth data has been RESET"))
    assert out[0].error_class is ErrorClass.DEPTH_RESET and out[0].generation_active
    assert isinstance(out[1], M.DepthResetEvent) and out[1].reason is M.ResetReason.IBKR_317
    (ev,) = norm(n, s.error(-1, 10197, "No market data during competing live session"))
    assert ev.error_class is ErrorClass.SESSION_CONFLICT and ev.stream is None
    (ev,) = norm(n, s.error(BBO, 10190, "Max number of tick-by-tick requests has been reached"))
    assert ev.stream is Stream.BBO and ev.generation_active and ev.error_class is ErrorClass.CAPACITY_EXCEEDED


def test_contract_resolution_failures():
    # zero matches
    s = RawScript()
    n = Normalizer()
    from tests.support import SPEC
    raws = [s.request("reqContractDetails", CONTRACT, **dict(SPEC.to_params())),
            s.contract_details(symbol="NQ"), s.contract_end()]
    out = [e for r in raws for e in n.normalize(r)]
    assert isinstance(out[-1], M.ContractFailedEvent) and "no contract matches" in out[-1].reason
    # two distinct matches -> ambiguous
    s = RawScript()
    n = Normalizer()
    raws = [s.request("reqContractDetails", CONTRACT, **dict(SPEC.to_params())),
            s.contract_details(), s.contract_details(con_id=1), s.contract_end()]
    out = [e for r in raws for e in n.normalize(r)]
    assert "ambiguous" in out[-1].reason
    # IBKR error 200 on the contract request
    s = RawScript()
    n = Normalizer()
    raws = [s.request("reqContractDetails", CONTRACT, **dict(SPEC.to_params())),
            s.error(CONTRACT, 200, "No security definition has been found")]
    out = [e for r in raws for e in n.normalize(r)]
    assert isinstance(out[-1], M.ContractFailedEvent)


def test_bad_market_rule_fails_contract():
    s = RawScript()
    n = Normalizer()
    from tests.support import SPEC
    raws = [s.request("reqContractDetails", CONTRACT, **dict(SPEC.to_params())), s.contract_details(),
            s.contract_end(), s.market_rule(67, ((0.0, 0.0),))]
    out = [e for r in raws for e in n.normalize(r)]
    assert isinstance(out[-1], M.ContractFailedEvent)


def test_request_failed_and_timer_and_control():
    n, s = boot()
    from hermes.ibkr import raw_events as R
    (ev,) = norm(n, s.add(R.RawRequestFailed, method="reqMktDepth", req_id=DEPTH, error="socket"))
    assert isinstance(ev, M.RequestFailedEvent) and ev.stream is Stream.DEPTH
    (ev,) = norm(n, s.add(R.RawTimerTick, due_mono_ns=5, coalesced=3))
    assert isinstance(ev, M.ClockTickEvent) and (ev.due_mono_ns, ev.coalesced) == (5, 3)
    (ev,) = norm(n, s.control("operator_retry"))
    assert isinstance(ev, M.ControlEvent) and ev.kind == "operator_retry"


def test_normalizer_is_deterministic():
    s = RawScript().bootstrap().seed_book()
    a = [e for r in s.events for e in Normalizer().normalize(r)]
    n1, n2 = Normalizer(), Normalizer()
    assert [e for r in s.events for e in n1.normalize(r)] == [e for r in s.events for e in n2.normalize(r)]
    assert len(a) > 0


def test_decimal_sizes_exact():
    n, s = boot()
    from hermes.ibkr import raw_events as R
    raw = s.add(R.RawMarketDepth, req_id=DEPTH, position=0, operation=0, side=1, price=21000.0,
                size=Decimal("3.000"), is_l2=False)
    (ev,) = norm(n, raw)
    assert ev.size == 3
