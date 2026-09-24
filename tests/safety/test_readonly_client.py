"""ReadOnlyClient safety tests (architecture §10, decision 11).

No TWS connection is used: a fake connection records every byte that would reach the socket.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import ibapi
import pytest
from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.message import OUT
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.server_versions import MAX_CLIENT_VER, MIN_SERVER_VER_PROTOBUF
from ibapi.wrapper import EWrapper

from hermes.ibkr import codes
from hermes.ibkr.readonly import (
    ALLOWED_METHODS,
    FORBIDDEN_METHODS,
    SUPPORTED_IBAPI_VERSION,
    ForbiddenMessageError,
    GuardedConnection,
    OrderApiForbiddenError,
    ReadOnlyClient,
    ReadOnlyViolation,
    UnrecognizedFrameError,
    blocked_method_names,
    check_outgoing_bytes,
)

SERVER_VERSIONS = [176, MIN_SERVER_VER_PROTOBUF - 1, MAX_CLIENT_VER]


class FakeConn:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def isConnected(self) -> bool:  # noqa: N802
        return True

    def sendMsg(self, msg: bytes) -> int:  # noqa: N802
        self.sent.append(bytes(msg))
        return len(msg)

    def disconnect(self) -> None:
        pass


class RecordingWrapper(EWrapper):
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[tuple] = []

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802,N803
        self.errors.append((reqId, errorCode, errorString))


def connected_client(server_version: int) -> tuple[ReadOnlyClient, FakeConn, RecordingWrapper]:
    wrapper = RecordingWrapper()
    client = ReadOnlyClient(wrapper)
    fake = FakeConn()
    client.conn = fake
    client.serverVersion_ = server_version
    client.connState = EClient.CONNECTED
    return client, fake, wrapper


def mnq() -> Contract:
    c = Contract()
    c.symbol = "MNQ"
    c.secType = "FUT"
    c.exchange = "CME"
    c.currency = "USD"
    c.lastTradeDateOrContractMonth = "202612"
    return c


def order_args_for(name: str) -> tuple:
    """Plausible arguments so a blocked stub is exercised the way real code would call it."""
    if name.startswith("placeOrder"):
        return (1, mnq(), Order()) if name == "placeOrder" else (object(),)
    if name.startswith("cancelOrder"):
        return (1, OrderCancel()) if name == "cancelOrder" else (object(),)
    if name.startswith("reqGlobalCancel"):
        return (OrderCancel(),) if name == "reqGlobalCancel" else (object(),)
    return ()


# ---------------------------------------------------------------------------
# Version pin and policy consistency
# ---------------------------------------------------------------------------

def test_ibapi_version_is_pinned():
    assert ibapi.__version__ == SUPPORTED_IBAPI_VERSION, (
        "ibapi changed: re-audit hermes/ibkr/readonly.py (methods, message ids, framing) before upgrading"
    )


def test_forbidden_and_allowed_are_disjoint():
    assert not (ALLOWED_METHODS & FORBIDDEN_METHODS)


def _eclient_methods() -> set[str]:
    return {n for n in dir(EClient) if not n.startswith("_") and callable(getattr(EClient, n))}


def test_policy_names_exist_in_ibapi():
    methods = _eclient_methods()
    assert ALLOWED_METHODS <= methods, f"stale allowlist entries: {sorted(ALLOWED_METHODS - methods)}"
    assert FORBIDDEN_METHODS <= methods, f"stale forbidden entries: {sorted(FORBIDDEN_METHODS - methods)}"


def test_every_eclient_method_is_allowed_or_blocked():
    blocked = blocked_method_names()
    for name in _eclient_methods():
        assert (name in ALLOWED_METHODS) != (name in blocked), name


def test_all_forbidden_methods_are_blocked():
    assert FORBIDDEN_METHODS <= blocked_method_names()


def test_default_deny_blocks_non_allowlisted_read_methods():
    # Read-only account/order queries are not needed in Phase C -> denied by default.
    for name in ("reqPositions", "reqOpenOrders", "reqAllOpenOrders", "reqExecutions",
                 "reqAccountUpdates", "reqIds", "reqCompletedOrders"):
        assert name in blocked_method_names(), name


def test_forbidden_msg_ids_match_ibapi():
    assert codes.OUT_PLACE_ORDER == OUT.PLACE_ORDER
    assert codes.OUT_CANCEL_ORDER == OUT.CANCEL_ORDER
    assert codes.OUT_REQ_AUTO_OPEN_ORDERS == OUT.REQ_AUTO_OPEN_ORDERS
    assert codes.OUT_REPLACE_FA == OUT.REPLACE_FA
    assert codes.OUT_EXERCISE_OPTIONS == OUT.EXERCISE_OPTIONS
    assert codes.OUT_REQ_GLOBAL_CANCEL == OUT.REQ_GLOBAL_CANCEL
    assert codes.OUT_UPDATE_CONFIG == OUT.UPDATE_CONFIG
    from ibapi.common import PROTOBUF_MSG_ID
    assert codes.PROTOBUF_MSG_ID_OFFSET == PROTOBUF_MSG_ID


def test_allowlist_call_graph_is_closed():
    """Every EClient method reachable (via self.X(...)) from an allowed method is itself allowed.

    Guarantees the allowlist cannot route into a blocked (e.g. order) method, and that
    connect()/run() will not hit a blocked stub at runtime.
    """
    methods = _eclient_methods()
    seen: set[str] = set()
    stack = sorted(ALLOWED_METHODS)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        src = textwrap.dedent(inspect.getsource(getattr(EClient, name)))
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "self" and node.attr in methods):
                assert node.attr in ALLOWED_METHODS, f"allowed EClient.{name} reaches non-allowed self.{node.attr}"
                stack.append(node.attr)


def test_refuses_unsupported_ibapi_version(monkeypatch):
    from hermes.ibkr import readonly
    monkeypatch.setattr(readonly.ibapi, "__version__", "99.0.0")
    with pytest.raises(readonly.UnsupportedIbapiVersionError):
        ReadOnlyClient(EWrapper())


# ---------------------------------------------------------------------------
# Layer 1: blocked methods raise and send nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("name", sorted(FORBIDDEN_METHODS))
def test_forbidden_method_raises_and_sends_nothing(name, server_version):
    client, fake, _ = connected_client(server_version)
    with pytest.raises(OrderApiForbiddenError):
        getattr(client, name)(*order_args_for(name))
    assert fake.sent == []
    assert len(client.readonly_violations) == 1


def test_blocked_method_raises_even_when_disconnected():
    client = ReadOnlyClient(EWrapper())
    with pytest.raises(OrderApiForbiddenError):
        client.placeOrder(1, mnq(), Order())


# ---------------------------------------------------------------------------
# Layer 2: message-id guard (catches unbound base-class bypass)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
@pytest.mark.parametrize("msg_id", sorted(codes.FORBIDDEN_OUTGOING_MSG_IDS))
def test_message_id_guard(msg_id, server_version):
    client, fake, _ = connected_client(server_version)
    with pytest.raises(ForbiddenMessageError):
        client.sendMsg(msg_id, "1\0")
    with pytest.raises(ForbiddenMessageError):
        client.sendMsgProtoBuf(msg_id + codes.PROTOBUF_MSG_ID_OFFSET, b"\x08\x01")
    with pytest.raises(ForbiddenMessageError):
        client.sendMsgProtoBuf(msg_id, b"\x08\x01")
    assert fake.sent == []
    assert len(client.readonly_violations) == 3


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_unbound_base_class_order_calls_are_blocked(server_version):
    """Bypassing layer 1 via the base class still sends nothing and is always latched.

    Note: on legacy (text) server versions ibapi catches the exception inside the request
    body and reports it via EWrapper.error — hence the latch requirement.
    """
    client, fake, wrapper = connected_client(server_version)
    bypasses = [
        lambda: EClient.placeOrder(client, 1, mnq(), Order()),
        lambda: EClient.cancelOrder(client, 1, OrderCancel()),
        lambda: EClient.reqGlobalCancel(client, OrderCancel()),
        lambda: EClient.reqAutoOpenOrders(client, True),
    ]
    for i, bypass in enumerate(bypasses, start=1):
        try:
            bypass()
        except ReadOnlyViolation:
            pass
        assert len(client.readonly_violations) == i
    assert fake.sent == []


# ---------------------------------------------------------------------------
# Layer 3: wire guard
# ---------------------------------------------------------------------------

def test_any_assigned_connection_is_wrapped():
    client = ReadOnlyClient(EWrapper())
    raw = FakeConn()
    client.conn = raw
    assert isinstance(client.conn, GuardedConnection)
    assert client.conn.inner is raw
    client.conn = None
    assert client.conn is None


@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_wire_guard_blocks_below_message_layer(server_version):
    client, fake, _ = connected_client(server_version)
    # Skip layers 1 and 2 entirely: unbound EClient.sendMsg writes straight to client.conn.
    with pytest.raises(ForbiddenMessageError):
        EClient.sendMsg(client, OUT.PLACE_ORDER, "1\0")
    with pytest.raises(ForbiddenMessageError):
        EClient.sendMsgProtoBuf(client, OUT.PLACE_ORDER + codes.PROTOBUF_MSG_ID_OFFSET, b"\x08\x01")
    assert fake.sent == []
    assert len(client.readonly_violations) == 2


def test_wire_guard_frame_parsing():
    from ibapi import comm
    # raw-int encoding and text encoding of a forbidden id
    for use_raw in (True, False):
        with pytest.raises(ForbiddenMessageError):
            check_outgoing_bytes(comm.make_msg(OUT.CANCEL_ORDER, use_raw, "1\0"))
        check_outgoing_bytes(comm.make_msg(OUT.REQ_MKT_DEPTH, use_raw, "1\0"))
    with pytest.raises(ForbiddenMessageError):
        check_outgoing_bytes(comm.make_msg_proto(OUT.PLACE_ORDER + codes.PROTOBUF_MSG_ID_OFFSET, b"x"))
    # a forbidden frame hidden after an allowed one in the same buffer
    buf = comm.make_msg(OUT.REQ_CURRENT_TIME, True, "1\0") + comm.make_msg(OUT.PLACE_ORDER, True, "1\0")
    with pytest.raises(ForbiddenMessageError):
        check_outgoing_bytes(buf)
    # handshake passes
    check_outgoing_bytes(b"API\0" + comm.make_initial_msg("v100..187"))
    # fail closed on garbage / truncation
    for bad in (b"", b"\x00\x00", b"\x00\x00\x00\x09abc", b"\x00\x00\x00\x03xyz", b"API\0\x00\x00\x00\x05hello"):
        with pytest.raises(UnrecognizedFrameError):
            check_outgoing_bytes(bad)


# ---------------------------------------------------------------------------
# Allowed Phase C requests still work (encode and pass every guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("server_version", SERVER_VERSIONS)
def test_allowed_market_data_requests_pass(server_version):
    client, fake, wrapper = connected_client(server_version)
    c = mnq()
    requests = [
        lambda: client.reqCurrentTime(),
        lambda: client.reqMarketDataType(1),
        lambda: client.reqContractDetails(1001, c),
        lambda: client.reqMarketRule(26),
        lambda: client.reqMktData(2001, c, "", False, False, []),
        lambda: client.cancelMktData(2001),
        lambda: client.reqTickByTickData(3001, c, "AllLast", 0, False),
        lambda: client.reqTickByTickData(3002, c, "BidAsk", 0, True),
        lambda: client.cancelTickByTickData(3001),
        lambda: client.reqMktDepth(4001, c, 10, False, []),
        lambda: client.cancelMktDepth(4001, False),
    ]
    for i, req in enumerate(requests):
        before = len(fake.sent)
        req()
        assert len(fake.sent) == before + 1, f"request #{i} did not send exactly one frame"
    assert wrapper.errors == []
    assert client.readonly_violations == ()
    for frame in fake.sent:
        check_outgoing_bytes(frame)
