"""Gateway, pipeline, adapter signature conformance, latency histograms."""

from __future__ import annotations

import inspect
import threading

import pytest
from ibapi.client import EClient
from ibapi.wrapper import EWrapper

from hermes.config import BookConfig, GatewayConfig, SessionConfig, SubscriptionsConfig
from hermes.core.latency import LatencyHistogram
from hermes.core.telemetry import Telemetry
from hermes.ibkr import raw_events as R
from hermes.ibkr.adapter import IbkrAdapter, RawPipeline, SingleWriterViolation
from hermes.ibkr.gateway import NotConnectedError, RateLimitedError, RequestGateway
from hermes.ibkr.normalizer import Normalizer
from hermes.ibkr.readonly import ReadOnlyClient
from hermes.market.engine import ALERT_INTERNAL_ERROR, MarketEngine
from hermes.market.snapshot import SnapshotPublisher
from tests.support import SPEC


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self):
        self.sent = []

    def isConnected(self):  # noqa: N802
        return True

    def sendMsg(self, msg):  # noqa: N802
        self.sent.append(bytes(msg))
        return len(msg)

    def disconnect(self):
        pass


def connected_client() -> tuple[ReadOnlyClient, FakeConn]:
    from ibapi.server_versions import MAX_CLIENT_VER
    c = ReadOnlyClient(EWrapper())
    fake = FakeConn()
    c.conn = fake
    c.serverVersion_ = MAX_CLIENT_VER
    c.connState = EClient.CONNECTED
    return c, fake


class Clock:
    def __init__(self):
        self.t = 1_000_000_000

    def __call__(self):
        self.t += 1000
        return self.t


def mnq_contract():
    from hermes.ibkr.session import subscription_contract
    return subscription_contract(770561201, "CME")


# ---------------------------------------------------------------------------
# RequestGateway
# ---------------------------------------------------------------------------

def test_gateway_requires_readonly_client():
    gw = RequestGateway(lambda *a, **k: None, GatewayConfig())
    with pytest.raises(TypeError):
        gw.bind(EClient(EWrapper()))  # type: ignore[arg-type]


def test_gateway_not_connected():
    gw = RequestGateway(lambda *a, **k: None, GatewayConfig())
    with pytest.raises(NotConnectedError):
        gw.req_current_time()


def test_gateway_records_true_send_time_before_send_and_unique_req_ids():
    posted = []
    client, fake = connected_client()
    clock = Clock()
    order = []

    def post(cls, **fields):
        order.append(("post", len(fake.sent)))
        posted.append((cls, fields))

    gw = RequestGateway(post, GatewayConfig(), mono_ns=clock, wall_ns=clock)
    gw.bind(client)
    ids = [gw.req_mkt_depth(1, mnq_contract(), 10, False),
           gw.req_tick_by_tick(1, mnq_contract(), "BidAsk"),
           gw.req_tick_by_tick(1, mnq_contract(), "AllLast"),
           gw.req_mkt_data(1, mnq_contract()),
           gw.req_contract_details(1, mnq_contract(), SPEC)]
    assert ids == sorted(ids) and len(set(ids)) == 5 and ids[0] == GatewayConfig().first_req_id
    assert len(fake.sent) == 5
    assert all(sent_before == i for i, (_, sent_before) in enumerate(order))   # posted BEFORE each send
    cls, f = posted[0]
    assert cls is R.RawRequestIssued and f["method"] == "reqMktDepth" and f["req_id"] == ids[0]
    assert f["sent_mono_ns"] > 0 and f["sent_wall_ns"] > 0
    assert dict(posted[1][1]["params"])["tick_type"] == "BidAsk"
    assert dict(posted[4][1]["params"])["symbol"] == "MNQ"
    with pytest.raises(ValueError):
        gw.req_tick_by_tick(1, mnq_contract(), "MidPoint")


def test_gateway_local_failure_is_recorded():
    posted = []
    client, _ = connected_client()
    gw = RequestGateway(lambda cls, **f: posted.append((cls, f)), GatewayConfig())
    gw.bind(client)

    def boom(*a, **k):
        raise OSError("socket gone")

    client.reqMktData = boom  # type: ignore[method-assign]
    assert gw.req_mkt_data(1, mnq_contract()) is None
    assert [c for c, _ in posted] == [R.RawRequestIssued, R.RawRequestFailed] and gw.failed == 1


def test_gateway_rate_limit_is_a_guard():
    client, fake = connected_client()
    t = {"now": 0}
    gw = RequestGateway(lambda *a, **k: None, GatewayConfig(max_requests_per_second=1.0, burst=2),
                        mono_ns=lambda: t["now"])
    gw.bind(client)
    gw.req_current_time()
    gw.req_current_time()
    with pytest.raises(RateLimitedError):
        gw.req_current_time()
    t["now"] += 1_000_000_000
    gw.req_current_time()
    assert len(fake.sent) == 3 and gw.rate_limited == 1


def test_gateway_endpoint_specific_limit():
    client, _ = connected_client()
    t = {"now": 0}
    gw = RequestGateway(lambda *a, **k: None, GatewayConfig(), mono_ns=lambda: t["now"],
                        endpoint_limits={"reqMktDepth": (0.1, 1)})
    gw.bind(client)
    gw.req_mkt_depth(1, mnq_contract(), 10, False)
    with pytest.raises(RateLimitedError):
        gw.req_mkt_depth(1, mnq_contract(), 10, False)
    gw.req_current_time()                        # other endpoints unaffected


# ---------------------------------------------------------------------------
# RawPipeline
# ---------------------------------------------------------------------------

class ListRecorder:
    def __init__(self):
        self.events = []

    def submit(self, ev):
        self.events.append(ev)
        return True


def pipeline(tick_ns=250_000_000):
    eng = MarketEngine(BookConfig(), SessionConfig(), SubscriptionsConfig())
    rec = ListRecorder()
    p = RawPipeline(Normalizer(), eng, Telemetry(), SnapshotPublisher(), rec, tick_interval_ns=tick_ns,
                    snapshot_interval_ns=1)
    return p, eng, rec


def test_pipeline_sequencing_pending_first_and_contiguous():
    p, _, rec = pipeline()
    p.post_local(R.RawControl, kind="connect_attempt")
    p.on_callback(1_000, 2_000, R.RawNextValidId, {"order_id": 1})
    p.on_callback(2_000, 3_000, R.RawCurrentTime, {"time": 5})
    kinds = [type(e).__name__ for e in rec.events]
    assert kinds == ["RawControl", "RawNextValidId", "RawCurrentTime"]
    assert [e.seq for e in rec.events] == [1, 2, 3]
    assert rec.events[0].recv_mono_ns == 1_000           # sequenced at the callback entry


def test_pipeline_timer_ticks_record_due_and_emission():
    p, _, rec = pipeline(tick_ns=100)
    p.on_callback(1_000, 1, R.RawCurrentTime, {"time": 1})      # arms the timer: due at 1100
    p.on_callback(1_050, 1, R.RawCurrentTime, {"time": 1})      # not due
    p.on_callback(1_450, 1, R.RawCurrentTime, {"time": 1})      # 4 intervals elapsed
    ticks = [e for e in rec.events if isinstance(e, R.RawTimerTick)]
    assert len(ticks) == 1
    assert (ticks[0].due_mono_ns, ticks[0].recv_mono_ns, ticks[0].coalesced) == (1_100, 1_450, 4)
    p.on_callback(1_520, 1, R.RawCurrentTime, {"time": 1})      # next due 1500
    assert [e for e in rec.events if isinstance(e, R.RawTimerTick)][-1].due_mono_ns == 1_500


def test_pipeline_never_raises_into_ibapi_and_fails_safe():
    p, eng, _ = pipeline()

    def boom(raw):
        raise ValueError("bug")

    p.normalizer.normalize = boom  # type: ignore[method-assign]
    p.on_callback(1, 1, R.RawCurrentTime, {"time": 1})         # must not raise
    assert p.internal_errors == 1 and ALERT_INTERNAL_ERROR in eng.alerts


def test_engine_cannot_be_written_outside_pipeline():
    p, eng, _ = pipeline()
    from hermes.market import events as M
    with pytest.raises(SingleWriterViolation):
        eng.on_event(M.HeartbeatEvent(seq=1, instrument_id=0, recv_mono_ns=1, recv_wall_ns=1, tws_time_s=1))


def test_pipeline_is_thread_safe_for_posting():
    p, _, rec = pipeline()
    threads = [threading.Thread(target=lambda: [p.post_local(R.RawControl, kind="x") for _ in range(200)])
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    p.on_callback(1, 1, R.RawCurrentTime, {"time": 1})
    assert len(rec.events) == 801 and [e.seq for e in rec.events] == list(range(1, 802))


def test_snapshot_published():
    p, _, _ = pipeline()
    p.on_callback(1, 1, R.RawNextValidId, {"order_id": 1})
    assert p.publisher.latest() is not None and p.publisher.latest().seq == 1


# ---------------------------------------------------------------------------
# Adapter conformance with ibapi's EWrapper (decoder calls callbacks positionally)
# ---------------------------------------------------------------------------

def test_adapter_callback_signatures_match_ewrapper():
    overridden = [n for n, f in vars(IbkrAdapter).items() if callable(f) and not n.startswith("_")]
    assert len(overridden) >= 15
    for name in overridden:
        ours = [p for p in inspect.signature(getattr(IbkrAdapter, name)).parameters.values()]
        theirs = [p for p in inspect.signature(getattr(EWrapper, name)).parameters.values()]
        assert len(ours) == len(theirs), name


def test_adapter_timestamps_first_and_converts_ibapi_objects():
    from decimal import Decimal
    from ibapi.common import TickAttribBidAsk, TickAttribLast
    from ibapi.contract import ContractDetails
    from ibapi.common import PriceIncrement

    p, _, rec = pipeline()
    a = IbkrAdapter(p)
    cd = ContractDetails()
    cd.contract.conId = 1
    cd.contract.symbol = "MNQ"
    cd.minTick = 0.25
    cd.marketRuleIds = "67"
    cd.validExchanges = "CME"
    a.contractDetails(5, cd)
    pi = PriceIncrement()
    pi.lowEdge, pi.increment = 0.0, 0.25
    a.marketRule(67, [pi])
    tal = TickAttribLast()
    tal.pastLimit = True
    a.tickByTickAllLast(7, 2, 1790000000, 21000.25, Decimal(3), tal, "CME", "")
    tba = TickAttribBidAsk()
    a.tickByTickBidAsk(8, 1790000000, 21000.0, 21000.25, Decimal(1), Decimal(2), tba)
    a.updateMktDepthL2(9, 0, "MM", 0, 1, 21000.0, Decimal(4), True)
    a.error(-1, 0, 2104, "Market data farm connection is OK:usfuture", "")
    names = [type(e).__name__ for e in rec.events]
    assert names == ["RawContractDetails", "RawMarketRule", "RawTickByTickAllLast", "RawTickByTickBidAsk",
                     "RawMarketDepth", "RawError"]
    assert rec.events[1].increments == ((0.0, 0.25),)
    assert rec.events[2].past_limit is True and rec.events[2].size == Decimal(3)
    assert rec.events[4].is_l2 and rec.events[4].market_maker == "MM"
    assert all(e.recv_mono_ns > 0 for e in rec.events)


# ---------------------------------------------------------------------------
# Latency histogram
# ---------------------------------------------------------------------------

def test_latency_histogram():
    h = LatencyHistogram()
    for ns in [1_000] * 98 + [100_000, 5_000_000]:
        h.record(ns)
    s = h.swap()
    assert s.count == 100 and s.max_ns == 5_000_000
    assert 1_000 <= s.p50_ns <= 2_048 and s.p99_ns <= 131_072
    assert h.count == 0 and h.swap().count == 0
    h.record(-5)
    assert h.summary().max_ns == 0
