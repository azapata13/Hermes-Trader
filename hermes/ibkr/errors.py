"""Classification of IBKR error / status codes into semantic ``ErrorClass`` values.

The table is deliberately explicit. Codes marked VERIFY are taken from IBKR documentation /
common usage and must be confirmed during live validation (see docs, "unresolved IBKR behavior").

Fail-safe rule (enforced by the engine, not here): an UNKNOWN / SUBSCRIPTION_REJECTED /
CAPACITY_EXCEEDED error carrying the reqId of an ACTIVE subscription generation makes that
stream UNAVAILABLE. INFO never changes stream state.
"""

from __future__ import annotations

from types import MappingProxyType

from hermes.market.events import ErrorClass

E = ErrorClass

ERROR_TABLE = MappingProxyType({
    # --- connectivity ---------------------------------------------------------------
    1100: E.CONNECTIVITY_LOST,            # Connectivity between IB and TWS has been lost
    1101: E.RESTORED_DATA_LOST,           # restored, data lost -> resubscribe everything
    1102: E.RESTORED_DATA_KEPT,           # restored, data maintained -> still revalidate
    1300: E.CONNECTIVITY_LOST,            # TWS socket port reset, connection dropped
    2110: E.SERVER_CONNECTIVITY_BROKEN,   # TWS <-> IB server connectivity broken
    502: E.CONNECT_FAILED,                # couldn't connect to TWS
    326: E.CONNECT_FAILED,                # client id already in use
    504: E.NOT_CONNECTED,
    # --- data farms -------------------------------------------------------------------
    2103: E.FARM_BROKEN,                  # market data farm connection is broken
    2104: E.FARM_OK,                      # market data farm connection is OK
    2105: E.INFO,                         # HMDS (historical) farm broken - not live data
    2106: E.INFO,                         # HMDS farm OK
    2107: E.INFO,                         # HMDS farm inactive
    2108: E.INFO,                         # market data farm inactive but available on demand
    2119: E.INFO,                         # market data farm is connecting
    2157: E.INFO,                         # sec-def farm broken (contract lookups may fail -> timeout)
    2158: E.INFO,                         # sec-def farm OK
    # --- market depth -------------------------------------------------------------------
    317: E.DEPTH_RESET,                   # "Market depth data has been RESET"
    316: E.DEPTH_HALTED,                  # VERIFY: "Market depth data has been HALTED"
    309: E.CAPACITY_EXCEEDED,             # max number of market depth requests reached
    # --- subscriptions / permissions ------------------------------------------------------
    101: E.CAPACITY_EXCEEDED,             # max number of tickers reached
    10190: E.CAPACITY_EXCEEDED,           # VERIFY: max number of tick-by-tick requests reached
    10189: E.SUBSCRIPTION_REJECTED,       # VERIFY: failed to request tick-by-tick data
    354: E.SUBSCRIPTION_REJECTED,         # requested market data is not subscribed
    10090: E.SUBSCRIPTION_REJECTED,       # part of requested market data is not subscribed
    10168: E.SUBSCRIPTION_REJECTED,       # not subscribed, delayed data not enabled
    322: E.SUBSCRIPTION_REJECTED,         # error processing request (e.g. duplicate id)
    10167: E.DATA_NOT_LIVE,               # displaying delayed market data
    10197: E.SESSION_CONFLICT,            # no market data during competing live session
    # --- contracts ------------------------------------------------------------------------
    200: E.CONTRACT_ERROR,                # no security definition found
    # --- benign -----------------------------------------------------------------------------
    300: E.INFO,                          # can't find EId (typically cancel of an old reqId)
})


def classify_error(code: int, message: str = "") -> ErrorClass:
    """Map an IBKR error code (+ message) to an ErrorClass."""
    if "read-only" in message.lower() or "read only" in message.lower():
        # TWS refused something because the API is read-only. Hermès never sends such
        # requests; seeing this is a safety event.
        return E.READONLY_REJECTED
    cls = ERROR_TABLE.get(code)
    if cls is not None:
        return cls
    if 2100 <= code <= 2199:
        return E.INFO                     # TWS warning/status band
    if code == 321:
        return E.SUBSCRIPTION_REJECTED    # error validating request
    return E.UNKNOWN


#: Classes that make an ACTIVE subscription generation unusable (fail safe).
SUBSCRIPTION_FATAL = frozenset({
    E.SUBSCRIPTION_REJECTED, E.CAPACITY_EXCEEDED, E.CONTRACT_ERROR, E.UNKNOWN,
})
