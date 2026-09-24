"""ReadOnlyClient — the only EClient Hermès may use in Phase C.

Defense in depth against any order-capable API call (architecture §10):

Layer 1 — method allowlist (default deny)
    Every public callable of ``ibapi.client.EClient`` that is not in ``ALLOWED_METHODS`` is
    replaced on this class by a stub raising ``OrderApiForbiddenError``. Methods added by a
    future ibapi release are therefore blocked automatically. ``FORBIDDEN_METHODS`` lists the
    order-capable / account-mutating methods that must never be allowlisted (tested).

Layer 2 — outgoing message-id guard
    ``sendMsg`` / ``sendMsgProtoBuf`` refuse forbidden outgoing message ids (PLACE_ORDER,
    CANCEL_ORDER, REQ_GLOBAL_CANCEL, EXERCISE_OPTIONS, REQ_AUTO_OPEN_ORDERS, REPLACE_FA,
    UPDATE_CONFIG — with or without the protobuf offset). This catches bypasses such as
    ``EClient.placeOrder(client, ...)`` (unbound base-class call).

Layer 3 — wire guard
    Any connection object assigned to ``client.conn`` is wrapped in ``GuardedConnection``.
    Every outbound buffer is parsed into frames and forbidden message ids are refused BEFORE
    bytes reach the socket. Unparseable frames are refused (fail closed).

Every blocked attempt is also LATCHED on the client (``readonly_violations``) because ibapi
swallows exceptions in several request paths and reports them via ``EWrapper.error``.

Plus: TWS "Read-Only API" stays enabled (outside this code), static AST tests, config
invariants. The ibapi version is pinned because layers 1–3 rely on its layout.

The client is composed with a separate EWrapper (``ReadOnlyClient(wrapper)``); do not
multiple-inherit EWrapper + ReadOnlyClient.
"""

from __future__ import annotations

import logging
import struct
from typing import Any, Callable

import ibapi
from ibapi.client import EClient

from hermes.ibkr.codes import base_msg_id, is_forbidden_msg_id

log = logging.getLogger("hermes.safety.readonly")

SUPPORTED_IBAPI_VERSION = "10.45.1"


class ReadOnlyViolation(RuntimeError):
    """Base class: an attempt to use order-capable / non-allowlisted TWS API functionality."""


class OrderApiForbiddenError(ReadOnlyViolation):
    """A non-allowlisted EClient method was called."""


class ForbiddenMessageError(ReadOnlyViolation):
    """A forbidden outgoing message id reached the message or wire layer."""


class UnrecognizedFrameError(ReadOnlyViolation):
    """An outgoing buffer could not be parsed; refused (fail closed)."""


class UnsupportedIbapiVersionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Method policy
# ---------------------------------------------------------------------------

#: Order-capable or account/configuration-mutating methods. Must NEVER be allowlisted.
FORBIDDEN_METHODS: frozenset[str] = frozenset({
    "placeOrder", "placeOrderProtoBuf",
    "cancelOrder", "cancelOrderProtoBuf",
    "reqGlobalCancel", "reqGlobalCancelProtoBuf",
    "exerciseOptions", "exerciseOptionsProtoBuf",
    "reqAutoOpenOrders", "reqAutoOpenOrdersProtoBuf",
    "replaceFA", "replaceFAProtoBuf",
    "updateConfigProtoBuf",
    "validateOrderParameters", "validateAttachedOrdersParameters",
})

#: Connection lifecycle / internals required by connect(), run(), disconnect().
_LIFECYCLE_METHODS = frozenset({
    "reset", "setConnState", "sendMsg", "sendMsgProtoBuf", "logRequest",
    "validateInvalidSymbols", "checkConnected", "useProtoBuf",
    "startApi", "startApiProtoBuf", "connect", "disconnect", "isConnected",
    "keyboardInterrupt", "keyboardInterruptHard", "setConnectOptions",
    "setOptionalCapabilities", "msgLoopTmo", "msgLoopRec", "run",
    "serverVersion", "twsConnectionTime",
})

#: Market-data / reference-data requests used by Phase C (and their protobuf variants).
_MARKET_DATA_METHODS = frozenset({
    "reqCurrentTime", "reqCurrentTimeProtoBuf",
    "reqCurrentTimeInMillis", "reqCurrentTimeInMillisProtoBuf",
    "setServerLogLevel", "setServerLogLevelProtoBuf",
    "reqMarketDataType", "reqMarketDataTypeProtoBuf",
    "reqContractDetails", "reqContractDataProtoBuf",
    "cancelContractData", "cancelContractDataProtoBuf",
    "reqMarketRule", "reqMarketRuleProtoBuf",
    "reqMktData", "reqMarketDataProtoBuf",
    "cancelMktData", "cancelMarketDataProtoBuf",
    "reqTickByTickData", "reqTickByTickDataProtoBuf",
    "cancelTickByTickData", "cancelTickByTickProtoBuf",
    "reqMktDepth", "reqMarketDepthProtoBuf",
    "cancelMktDepth", "cancelMarketDepthProtoBuf",
    "reqMktDepthExchanges", "reqMarketDepthExchangesProtoBuf",
    "reqHistoricalData", "reqHistoricalDataProtoBuf",
    "cancelHistoricalData", "cancelHistoricalDataProtoBuf",
    "reqHistoricalTicks", "reqHistoricalTicksProtoBuf",
    "cancelHistoricalTicks", "cancelHistoricalTicksProtoBuf",
    "reqHeadTimeStamp", "reqHeadTimestampProtoBuf",
    "cancelHeadTimeStamp", "cancelHeadTimestampProtoBuf",
})

ALLOWED_METHODS: frozenset[str] = _LIFECYCLE_METHODS | _MARKET_DATA_METHODS

assert not (ALLOWED_METHODS & FORBIDDEN_METHODS), "forbidden method allowlisted"


def _eclient_public_callables() -> list[str]:
    return sorted(
        name for name in dir(EClient)
        if not name.startswith("_") and callable(getattr(EClient, name))
    )


def _make_blocked(name: str) -> Callable[..., Any]:
    def blocked(self: Any, *args: Any, **kwargs: Any) -> Any:
        err = OrderApiForbiddenError(
            f"EClient.{name} is not permitted in Hermès Phase C (read-only market intelligence)"
        )
        _record_violation(self, err)
        raise err

    blocked.__name__ = name
    blocked.__qualname__ = f"ReadOnlyClient.{name}"
    blocked.__hermes_blocked__ = True  # type: ignore[attr-defined]
    return blocked


# ---------------------------------------------------------------------------
# Wire guard
# ---------------------------------------------------------------------------

_HANDSHAKE_PREFIX = b"API\0"


def _frame_msg_id(payload: bytes) -> int:
    """Extract the outgoing message id from one frame payload (text or raw-int encoding)."""
    if len(payload) >= 4 and payload[0] == 0:
        # raw int encoding (server >= MIN_SERVER_VER_PROTOBUF): 4-byte big-endian id
        return int.from_bytes(payload[:4], "big")
    nul = payload.find(b"\0")
    token = payload[:nul] if nul >= 0 else b""
    if not token or not token.isdigit():
        raise UnrecognizedFrameError(f"cannot determine outgoing message id (payload head {payload[:8]!r})")
    return int(token)


def check_outgoing_bytes(data: bytes) -> None:
    """Raise if ``data`` contains any forbidden (or unparseable) outgoing frame."""
    buf = bytes(data)
    off = 0
    if buf.startswith(_HANDSHAKE_PREFIX):
        # Initial handshake: "API\0" + one length-prefixed version string "v<min>..<max>[ opts]".
        off = len(_HANDSHAKE_PREFIX)
        if len(buf) < off + 4:
            raise UnrecognizedFrameError("truncated handshake")
        (n,) = struct.unpack_from("!I", buf, off)
        body = buf[off + 4: off + 4 + n]
        if len(body) != n or not body.startswith(b"v"):
            raise UnrecognizedFrameError("malformed handshake")
        off += 4 + n
    if off == len(buf) and off > 0:
        return
    if not buf:
        raise UnrecognizedFrameError("empty outgoing buffer")
    while off < len(buf):
        if len(buf) - off < 4:
            raise UnrecognizedFrameError("truncated frame header")
        (n,) = struct.unpack_from("!I", buf, off)
        payload = buf[off + 4: off + 4 + n]
        if len(payload) != n or n == 0:
            raise UnrecognizedFrameError("truncated or empty frame")
        msg_id = _frame_msg_id(payload)
        if is_forbidden_msg_id(msg_id):
            raise ForbiddenMessageError(f"outgoing message id {msg_id} (base {base_msg_id(msg_id)}) is forbidden")
        off += 4 + n


def _record_violation(owner: Any, err: ReadOnlyViolation) -> None:
    """Log and latch a violation on its owner.

    ibapi wraps many request bodies in ``except Exception`` and reports failures through
    ``EWrapper.error`` instead of re-raising, so an exception alone is not a reliable signal.
    Every violation is therefore also latched on the client (``readonly_violations``); the
    engine/watchdog treat a non-empty latch as a critical safety event.
    """
    log.critical("READ-ONLY VIOLATION: %s", err)
    if owner is not None:
        try:
            owner.__dict__.setdefault("_hermes_violations", []).append(str(err))
        except AttributeError:  # pragma: no cover
            pass


class GuardedConnection:
    """Transparent proxy around ibapi.connection.Connection that inspects every outbound buffer."""

    __slots__ = ("_inner", "_owner")

    def __init__(self, inner: Any, owner: Any = None) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_owner", owner)

    def sendMsg(self, msg: bytes) -> Any:  # noqa: N802 (ibapi naming)
        try:
            check_outgoing_bytes(msg)
        except ReadOnlyViolation as err:
            _record_violation(self._owner, err)
            raise
        return self._inner.sendMsg(msg)

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ReadOnlyClient(EClient):
    """EClient restricted to allowlisted, non-order market-data functionality."""

    def __init__(self, wrapper: Any) -> None:
        if ibapi.__version__ != SUPPORTED_IBAPI_VERSION:
            raise UnsupportedIbapiVersionError(
                f"ibapi {ibapi.__version__} installed; ReadOnlyClient is audited for "
                f"{SUPPORTED_IBAPI_VERSION}. Re-audit hermes/ibkr/readonly.py before upgrading."
            )
        EClient.__init__(self, wrapper)

    # ---- Layer 3: every connection assigned by ibapi is wrapped ----
    @property
    def conn(self) -> Any:
        return self.__dict__.get("_hermes_conn")

    @conn.setter
    def conn(self, value: Any) -> None:
        if value is not None and not isinstance(value, GuardedConnection):
            value = GuardedConnection(value, owner=self)
        self.__dict__["_hermes_conn"] = value

    @property
    def readonly_violations(self) -> tuple[str, ...]:
        """Latched record of every blocked attempt on this client (never cleared)."""
        return tuple(self.__dict__.get("_hermes_violations", ()))

    # ---- Layer 2: message-id guard ----
    def sendMsg(self, msgId: int, msg: str) -> None:  # noqa: N802,N803
        if is_forbidden_msg_id(msgId):
            err = ForbiddenMessageError(f"outgoing message id {msgId} is forbidden (sendMsg)")
            _record_violation(self, err)
            raise err
        EClient.sendMsg(self, msgId, msg)

    def sendMsgProtoBuf(self, msgId: int, msg: bytes) -> None:  # noqa: N802,N803
        if is_forbidden_msg_id(msgId):
            err = ForbiddenMessageError(f"outgoing message id {msgId} is forbidden (sendMsgProtoBuf)")
            _record_violation(self, err)
            raise err
        EClient.sendMsgProtoBuf(self, msgId, msg)


# ---- Layer 1: default-deny every non-allowlisted EClient callable ----
for _name in _eclient_public_callables():
    if _name not in ALLOWED_METHODS:
        setattr(ReadOnlyClient, _name, _make_blocked(_name))
del _name


def blocked_method_names() -> frozenset[str]:
    return frozenset(
        n for n in dir(ReadOnlyClient)
        if getattr(getattr(ReadOnlyClient, n, None), "__hermes_blocked__", False)
    )
