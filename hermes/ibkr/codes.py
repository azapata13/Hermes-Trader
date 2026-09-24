"""IBKR numeric codes and their mapping to internal enums.

Kept free of ``ibapi`` imports; values are cross-checked against ``ibapi.message.OUT``
and the ibapi documentation by tests.
"""

from __future__ import annotations

from types import MappingProxyType

from hermes.market.events import BookSide, DepthOp

# ---------------------------------------------------------------------------
# Market depth (reqMktDepth callbacks)
# ---------------------------------------------------------------------------

IBKR_DEPTH_SIDE_ASK = 0
IBKR_DEPTH_SIDE_BID = 1

IBKR_DEPTH_SIDE = MappingProxyType({
    IBKR_DEPTH_SIDE_ASK: BookSide.ASK,
    IBKR_DEPTH_SIDE_BID: BookSide.BID,
})

IBKR_DEPTH_OP = MappingProxyType({
    0: DepthOp.INSERT,
    1: DepthOp.UPDATE,
    2: DepthOp.DELETE,
})


def depth_side(code: int) -> BookSide | None:
    """IBKR depth side code -> BookSide, or None if the code is invalid."""
    return IBKR_DEPTH_SIDE.get(code)


def depth_op(code: int) -> DepthOp | None:
    """IBKR depth operation code -> DepthOp, or None if the code is invalid."""
    return IBKR_DEPTH_OP.get(code)


# ---------------------------------------------------------------------------
# Market data type (marketDataType callback)
# ---------------------------------------------------------------------------

MARKET_DATA_TYPE_LIVE = 1
MARKET_DATA_TYPE_FROZEN = 2
MARKET_DATA_TYPE_DELAYED = 3
MARKET_DATA_TYPE_DELAYED_FROZEN = 4

# ---------------------------------------------------------------------------
# Error / status codes handled explicitly (classification implemented in C3)
# ---------------------------------------------------------------------------

ERR_DEPTH_RESET = 317
ERR_CONNECTIVITY_LOST = 1100
ERR_RESTORED_DATA_LOST = 1101
ERR_RESTORED_DATA_KEPT = 1102
ERR_MAX_DEPTH_REQUESTS = 309
ERR_NOT_SUBSCRIBED = 354
ERR_PARTIALLY_SUBSCRIBED = 10090
ERR_DELAYED_DATA = 10167
ERR_MARKET_DATA_SESSION_CONFLICT = 10197
FARM_BROKEN_CODES = frozenset({2103, 2105, 2157})
FARM_OK_CODES = frozenset({2104, 2106, 2158})
ERR_TWS_SERVER_CONNECTIVITY_BROKEN = 2110

# ---------------------------------------------------------------------------
# Outgoing message ids that must NEVER be sent in Phase C.
# Values mirror ibapi.message.OUT (10.45.1); protobuf variants are id + PROTOBUF_MSG_ID.
# ---------------------------------------------------------------------------

OUT_PLACE_ORDER = 3
OUT_CANCEL_ORDER = 4
OUT_REQ_AUTO_OPEN_ORDERS = 15
OUT_REPLACE_FA = 19
OUT_EXERCISE_OPTIONS = 21
OUT_REQ_GLOBAL_CANCEL = 58
OUT_UPDATE_CONFIG = 109

PROTOBUF_MSG_ID_OFFSET = 200

FORBIDDEN_OUTGOING_MSG_IDS = frozenset({
    OUT_PLACE_ORDER,
    OUT_CANCEL_ORDER,
    OUT_REQ_AUTO_OPEN_ORDERS,
    OUT_REPLACE_FA,
    OUT_EXERCISE_OPTIONS,
    OUT_REQ_GLOBAL_CANCEL,
    OUT_UPDATE_CONFIG,
})


def base_msg_id(msg_id: int) -> int:
    """Strip the protobuf offset from an outgoing message id."""
    return msg_id - PROTOBUF_MSG_ID_OFFSET if msg_id > PROTOBUF_MSG_ID_OFFSET else msg_id


def is_forbidden_msg_id(msg_id: int) -> bool:
    return base_msg_id(msg_id) in FORBIDDEN_OUTGOING_MSG_IDS
