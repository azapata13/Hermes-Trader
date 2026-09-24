"""Phase B diagnostic — ported to ReadOnlyClient in C3 (no direct EClient use).

Standalone script; run from the repository root:
    python tools/phase_b/mnq_live_test.py
READ-ONLY: uses hermes.ibkr.readonly.ReadOnlyClient (order methods blocked in 3 layers).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import threading
import time
from decimal import Decimal

from hermes.ibkr.readonly import ReadOnlyClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 102

CONTRACT_REQ_ID = 1001
MARKET_DATA_REQ_ID = 2001


class HermesMarketTest(EWrapper):
    def __init__(self):
        EWrapper.__init__(self)

        self.connected_event = threading.Event()
        self.contract_event = threading.Event()

        self.contracts = []
        self.selected_contract = None

        self.bid = None
        self.ask = None
        self.last = None

        self.bid_size = None
        self.ask_size = None
        self.last_size = None

    # ---------- CONNECTION ----------

    def nextValidId(self, orderId: int):
        print("✅ Hermes connected to TWS")
        self.connected_event.set()

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        informational_codes = {
            2104,  # Market data farm connection OK
            2106,  # HMDS connection OK
            2158,  # Sec-def connection OK
        }

        if errorCode in informational_codes:
            print(f"ℹ️ IBKR status {errorCode}: {errorString}")
            return

        print(
            f"⚠️ IBKR | reqId={reqId} | "
            f"code={errorCode} | {errorString}"
        )

    # ---------- CONTRACT DISCOVERY ----------

    def contractDetails(self, reqId, contractDetails):
        contract = contractDetails.contract
        self.contracts.append(contract)

        print("\n🔎 Contract found")
        print(f"   Symbol:       {contract.symbol}")
        print(f"   Local symbol: {contract.localSymbol}")
        print(f"   Expiry:       {contract.lastTradeDateOrContractMonth}")
        print(f"   Exchange:     {contract.exchange}")
        print(f"   Currency:     {contract.currency}")
        print(f"   conId:        {contract.conId}")

    def contractDetailsEnd(self, reqId):
        print(f"\n✅ Contract search complete ({len(self.contracts)} match(es))")

        if self.contracts:
            # Because our request is already specific to Dec 18 2026,
            # normally there should be one exact match.
            self.selected_contract = self.contracts[0]

        self.contract_event.set()

    # ---------- MARKET DATA ----------

    def marketDataType(self, reqId, marketDataType):
        labels = {
            1: "LIVE",
            2: "FROZEN",
            3: "DELAYED",
            4: "DELAYED-FROZEN",
        }

        label = labels.get(marketDataType, f"UNKNOWN ({marketDataType})")
        print(f"\n📡 Market data type: {label}")

    def tickPrice(self, reqId, tickType, price, attrib):
        tick_name = TickTypeEnum.toStr(tickType)

        if tick_name == "BID":
            self.bid = price
        elif tick_name == "ASK":
            self.ask = price
        elif tick_name == "LAST":
            self.last = price
        else:
            return

        self.print_market()

    def tickSize(self, reqId, tickType, size: Decimal):
        tick_name = TickTypeEnum.toStr(tickType)

        if tick_name == "BID_SIZE":
            self.bid_size = size
        elif tick_name == "ASK_SIZE":
            self.ask_size = size
        elif tick_name == "LAST_SIZE":
            self.last_size = size
        else:
            return

        self.print_market()

    def print_market(self):
        spread = None

        if self.bid is not None and self.ask is not None:
            spread = self.ask - self.bid

        print(
            "\r"
            f"BID {self.bid} x {self.bid_size}   |   "
            f"ASK {self.ask} x {self.ask_size}   |   "
            f"LAST {self.last} x {self.last_size}   |   "
            f"SPREAD {spread}",
            end="",
            flush=True,
        )


def build_mnq_dec_2026():
    contract = Contract()

    contract.symbol = "MNQ"
    contract.secType = "FUT"
    contract.exchange = "CME"
    contract.currency = "USD"

    # December 18, 2026 contract
    contract.lastTradeDateOrContractMonth = "20261218"

    return contract


def main():
    app = HermesMarketTest()
    client = ReadOnlyClient(app)  # the only permitted EClient (read-only guard)

    print("================================================")
    print(" HERMES — MNQ LIVE MARKET DATA TEST")
    print(" READ-ONLY / NO ORDERS")
    print("================================================")

    print("\nConnecting to TWS...")

    client.connect(
        HOST,
        PORT,
        clientId=CLIENT_ID
    )

    thread = threading.Thread(
        target=client.run,
        daemon=True
    )
    thread.start()

    if not app.connected_event.wait(timeout=8):
        print("\n❌ Could not connect to TWS")
        client.disconnect()
        return

    # ----- Resolve the exact futures contract -----

    print("\n🔍 Looking for MNQ Dec 18 2026...")

    client.reqContractDetails(
        CONTRACT_REQ_ID,
        build_mnq_dec_2026()
    )

    if not app.contract_event.wait(timeout=10):
        print("\n❌ Contract lookup timed out")
        client.disconnect()
        return

    if app.selected_contract is None:
        print("\n❌ MNQ Dec 2026 contract not found")
        client.disconnect()
        return

    c = app.selected_contract

    print("\n🎯 Using:")
    print(f"   {c.localSymbol}")
    print(f"   conId: {c.conId}")
    print(f"   expiry: {c.lastTradeDateOrContractMonth}")

    # Explicitly request LIVE data.
    # IBKR market-data type 1 = Live.
    client.reqMarketDataType(1)

    print("\n📡 Starting real-time BID / ASK / LAST...")
    print("   Listening for 20 seconds...\n")

    client.reqMktData(
        MARKET_DATA_REQ_ID,
        c,
        "",       # Generic tick list
        False,    # Streaming, not snapshot
        False,    # Not regulatory snapshot
        []
    )

    try:
        time.sleep(20)
    except KeyboardInterrupt:
        pass

    client.cancelMktData(MARKET_DATA_REQ_ID)

    print("\n\n🛑 Market data stream stopped.")

    client.disconnect()
    thread.join(timeout=2)

    print("✅ Phase B test complete.")


if __name__ == "__main__":
    main()
