"""Phase B diagnostic — ported to ReadOnlyClient in C3 (no direct EClient use).

Standalone script; run from the repository root:
    python tools/phase_b/connection_test.py
READ-ONLY: uses hermes.ibkr.readonly.ReadOnlyClient (order methods blocked in 3 layers).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import threading
import time

from hermes.ibkr.readonly import ReadOnlyClient
from ibapi.wrapper import EWrapper


class HermesIBKR(EWrapper):
    def __init__(self):
        EWrapper.__init__(self)
        self.connected_event = threading.Event()

    def nextValidId(self, orderId: int):
        print("✅ Connected to TWS")
        print(f"✅ Next valid order ID: {orderId}")
        self.connected_event.set()

    def managedAccounts(self, accountsList: str):
        count = len([a for a in accountsList.split(",") if a.strip()])
        print(f"✅ Managed account detected ({count})")

    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson=""
    ):
        # IBKR also routes informational status messages through error()
        if errorCode in (2104, 2106, 2158):
            print(f"ℹ️ IBKR status {errorCode}: {errorString}")
        else:
            print(
                f"⚠️ IBKR | reqId={reqId} | "
                f"code={errorCode} | {errorString}"
            )

        if advancedOrderRejectJson:
            print(f"Advanced reject: {advancedOrderRejectJson}")


def main():
    app = HermesIBKR()
    client = ReadOnlyClient(app)  # the only permitted EClient (read-only guard)

    print("Connecting Hermes → TWS @ 127.0.0.1:7496...")

    client.connect(
        "127.0.0.1",
        7496,
        clientId=101
    )

    api_thread = threading.Thread(
        target=client.run,
        daemon=True
    )
    api_thread.start()

    if not app.connected_event.wait(timeout=8):
        print("❌ Connection timeout")
        client.disconnect()
        return

    time.sleep(2)

    print(f"✅ API socket alive: {client.isConnected()}")

    client.disconnect()
    api_thread.join(timeout=2)

    print("✅ Hermes ↔ IBKR connection test successful")


if __name__ == "__main__":
    main()
