import threading
import time
from datetime import datetime
from decimal import Decimal

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract


HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 103

CONTRACT_REQ_ID = 1001
TRADES_REQ_ID = 3001
DEPTH_REQ_ID = 4001

DEPTH_ROWS = 10


class HermesOrderFlow(EWrapper, EClient):

    def __init__(self):
        EClient.__init__(self, self)

        self.connected_event = threading.Event()
        self.contract_event = threading.Event()
        self.stop_event = threading.Event()

        self.selected_contract = None

        self.bids = []
        self.asks = []

        self.book_lock = threading.Lock()

        self.trade_count = 0
        self.last_trade_price = None
        self.last_trade_size = None

    # ============================================================
    # CONNECTION
    # ============================================================

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

        informational = {
            2104,
            2106,
            2158,
        }

        if errorCode in informational:
            return

        # IMPORTANT:
        # IBKR code 317 means market depth was RESET.
        # Our local book MUST therefore be emptied.
        if errorCode == 317:
            print("\n♻️ IBKR DEPTH RESET — clearing local order book")

            with self.book_lock:
                self.bids.clear()
                self.asks.clear()

            return

        print(
            f"\n⚠️ IBKR | reqId={reqId} | "
            f"code={errorCode} | {errorString}"
        )

    # ============================================================
    # CONTRACT
    # ============================================================

    def contractDetails(self, reqId, contractDetails):
        c = contractDetails.contract

        # Keep the exact MNQ contract resolved by IBKR.
        if c.symbol == "MNQ":
            self.selected_contract = c

            print(
                f"🎯 CONTRACT: "
                f"{c.localSymbol} | "
                f"conId={c.conId} | "
                f"expiry={c.lastTradeDateOrContractMonth}"
            )

    def contractDetailsEnd(self, reqId):
        self.contract_event.set()

    # ============================================================
    # TIME & SALES
    # ============================================================

    def tickByTickAllLast(
        self,
        reqId,
        tickType,
        timestamp,
        price,
        size: Decimal,
        tickAttribLast,
        exchange,
        specialConditions
    ):

        self.trade_count += 1
        self.last_trade_price = price
        self.last_trade_size = size

        ts = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")

        print(
            f"\n⚡ TRADE #{self.trade_count:<5} "
            f"{ts} | "
            f"{price:>10.2f} | "
            f"{str(size):>5} contracts | "
            f"{exchange}"
        )

    # ============================================================
    # LEVEL II
    # ============================================================

    def updateMktDepth(
        self,
        reqId,
        position,
        operation,
        side,
        price,
        size
    ):
        self._update_book(
            position,
            operation,
            side,
            price,
            size,
            source=""
        )

    def updateMktDepthL2(
        self,
        reqId,
        position,
        marketMaker,
        operation,
        side,
        price,
        size,
        isSmartDepth
    ):
        self._update_book(
            position,
            operation,
            side,
            price,
            size,
            source=marketMaker
        )

    def _update_book(
        self,
        position,
        operation,
        side,
        price,
        size,
        source=""
    ):

        # IBKR:
        # side 0 = ASK
        # side 1 = BID
        #
        # operation:
        # 0 = insert
        # 1 = update
        # 2 = delete

        with self.book_lock:

            book = self.asks if side == 0 else self.bids

            row = {
                "price": float(price),
                "size": float(size),
                "source": source,
            }

            try:

                if operation == 0:
                    # Insert row
                    if position <= len(book):
                        book.insert(position, row)

                elif operation == 1:
                    # Update existing row
                    if position < len(book):
                        book[position] = row

                elif operation == 2:
                    # Delete row
                    if position < len(book):
                        book.pop(position)

                # Never keep more than requested depth
                if len(book) > DEPTH_ROWS:
                    del book[DEPTH_ROWS:]

            except Exception as exc:
                print(f"\n⚠️ Book update error: {exc}")

    # ============================================================
    # DISPLAY
    # ============================================================

    def display_book_loop(self):

        while not self.stop_event.is_set():

            time.sleep(0.75)

            with self.book_lock:
                bids = list(self.bids)
                asks = list(self.asks)

            if not bids and not asks:
                continue

            print("\n")
            print("=" * 56)
            print("              HERMES — MNQ LEVEL II")
            print("=" * 56)

            print("ASK — SELLERS")
            print("-" * 56)

            # Highest displayed ask first,
            # closest ask nearest the center.
            for row in reversed(asks[:8]):
                print(
                    f"ASK  "
                    f"{row['price']:>10.2f}   "
                    f"{row['size']:>7.0f}"
                )

            print("-" * 56)

            if asks:
                best_ask = asks[0]["price"]
            else:
                best_ask = None

            if bids:
                best_bid = bids[0]["price"]
            else:
                best_bid = None

            spread = None

            if best_bid is not None and best_ask is not None:
                spread = best_ask - best_bid

            print(
                f"BEST BID: {best_bid}  |  "
                f"BEST ASK: {best_ask}  |  "
                f"SPREAD: {spread}"
            )

            print("-" * 56)

            print("BID — BUYERS")

            for row in bids[:8]:
                print(
                    f"BID  "
                    f"{row['price']:>10.2f}   "
                    f"{row['size']:>7.0f}"
                )

            print("=" * 56)


def build_mnq_dec_2026():

    c = Contract()

    c.symbol = "MNQ"
    c.secType = "FUT"
    c.exchange = "CME"
    c.currency = "USD"
    c.lastTradeDateOrContractMonth = "20261218"

    return c


def main():

    app = HermesOrderFlow()

    print()
    print("================================================")
    print(" HERMES — ORDER FLOW TEST")
    print(" TIME & SALES + LEVEL II")
    print(" READ-ONLY — ZERO ORDER EXECUTION")
    print("================================================")
    print()

    app.connect(
        HOST,
        PORT,
        clientId=CLIENT_ID
    )

    api_thread = threading.Thread(
        target=app.run,
        daemon=True
    )
    api_thread.start()

    if not app.connected_event.wait(timeout=8):
        print("❌ Connection failed")
        app.disconnect()
        return

    # ------------------------------------------------------------
    # Resolve MNQ contract
    # ------------------------------------------------------------

    print("🔍 Resolving MNQ Dec 2026...")

    app.reqContractDetails(
        CONTRACT_REQ_ID,
        build_mnq_dec_2026()
    )

    if not app.contract_event.wait(timeout=10):
        print("❌ Contract lookup timeout")
        app.disconnect()
        return

    if app.selected_contract is None:
        print("❌ MNQ contract not found")
        app.disconnect()
        return

    contract = app.selected_contract

    # ------------------------------------------------------------
    # Force live market data
    # ------------------------------------------------------------

    app.reqMarketDataType(1)

    # ------------------------------------------------------------
    # TIME & SALES
    # ------------------------------------------------------------

    print("⚡ Starting Time & Sales...")

    app.reqTickByTickData(
        TRADES_REQ_ID,
        contract,
        "AllLast",
        0,
        False
    )

    # ------------------------------------------------------------
    # LEVEL II
    # ------------------------------------------------------------

    print("📚 Starting CME Level II...")

    app.reqMktDepth(
        DEPTH_REQ_ID,
        contract,
        DEPTH_ROWS,
        False,
        []
    )

    display_thread = threading.Thread(
        target=app.display_book_loop,
        daemon=True
    )

    display_thread.start()

    print()
    print("✅ HERMES ORDER FLOW IS LIVE")
    print("Press CTRL+C to stop.")
    print()

    try:

        while True:
            time.sleep(1)

    except KeyboardInterrupt:

        print("\n🛑 Stopping Hermes market feeds...")

    # ------------------------------------------------------------
    # CLEAN SHUTDOWN
    # ------------------------------------------------------------

    app.stop_event.set()

    app.cancelTickByTickData(TRADES_REQ_ID)
    app.cancelMktDepth(
        DEPTH_REQ_ID,
        False
    )

    time.sleep(0.5)

    app.disconnect()

    api_thread.join(timeout=2)

    print("✅ Hermes disconnected cleanly.")


if __name__ == "__main__":
    main()
