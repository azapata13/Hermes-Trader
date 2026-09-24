import threading
import time

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


class HermesIBKR(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
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

    print("Connecting Hermes → TWS @ 127.0.0.1:7496...")

    app.connect(
        "127.0.0.1",
        7496,
        clientId=101
    )

    api_thread = threading.Thread(
        target=app.run,
        daemon=True
    )
    api_thread.start()

    if not app.connected_event.wait(timeout=8):
        print("❌ Connection timeout")
        app.disconnect()
        return

    time.sleep(2)

    print(f"✅ API socket alive: {app.isConnected()}")

    app.disconnect()
    api_thread.join(timeout=2)

    print("✅ Hermes ↔ IBKR connection test successful")


if __name__ == "__main__":
    main()
