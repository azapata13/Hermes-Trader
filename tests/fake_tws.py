"""A minimal fake TWS speaking the real ibapi 10.45 wire protocol (protobuf messages).

Lets end-to-end tests exercise the OFFICIAL client path — ReadOnlyClient.connect handshake,
EReader thread, EClient.run dispatch, real decoder, our adapter — without TWS.

The server auto-responds like a tiny exchange simulator:
    START_API        -> NEXT_VALID_ID
    reqCurrentTime   -> CURRENT_TIME
    reqContractDetails -> CONTRACT_DATA (MNQZ6) + CONTRACT_DATA_END
    reqMarketRule    -> MARKET_RULE (0 -> 0.25)
    reqMktDepth      -> 5 bid + 5 ask inserts
    reqTickByTickData BidAsk -> one BidAsk;  AllLast -> one trade
    reqMktData       -> MARKET_DATA_TYPE(1) + BID/ASK ticks
Tests can inject arbitrary messages (errors, late callbacks from old reqIds, ...).
Every frame received from the client is kept (to assert that no order message was sent).
"""

from __future__ import annotations

import socket
import struct
import threading
import time

from ibapi.message import IN, OUT
from ibapi.protobuf import (
    ContractData_pb2, ContractDataEnd_pb2, ContractDataRequest_pb2, CurrentTime_pb2, ErrorMessage_pb2,
    MarketDataRequest_pb2, MarketDataType_pb2, MarketDepth_pb2, MarketDepthRequest_pb2, MarketRule_pb2,
    MarketRuleRequest_pb2, NextValidId_pb2, StartApiRequest_pb2, TickByTickData_pb2, TickByTickRequest_pb2,
    TickPrice_pb2,
)

PB = 200
SERVER_VERSION = 223


class FakeTws:
    def __init__(self, base_price: float = 21000.0, auto: bool = True) -> None:
        self.base = base_price
        self.auto = auto
        self.conflict_mode = False        # answer every reqMktData with error 10197 instead of data
        self.send_next_valid_id = True    # False: accept the API session but never send nextValidId
        self.rejected_client_ids: list[int] = []   # like TWS: a clientId already in use gets 326 + close
        self.start_api_client_ids: list[int] = []
        self._client_ids: set[int] = set()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.conn: socket.socket | None = None
        self.received: list[tuple[int, bytes]] = []      # (msg_id incl. protobuf offset, payload)
        self.requests: dict[str, list[int]] = {}         # method -> reqIds seen
        self.tbt_types: dict[int, str] = {}
        self.connections = 0
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._serve, name="fake-tws", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ server loop
    def _serve(self) -> None:
        # one thread per socket, like TWS: a second API client can connect while the first is up
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), name="fake-tws-conn", daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        client_id: int | None = None
        try:
            conn.settimeout(None)
            assert self._recv_exact(conn, 4) == b"API\0"
            (n,) = struct.unpack("!I", self._recv_exact(conn, 4))
            self._recv_exact(conn, n)                                    # "v100..223"
            hello = f"{SERVER_VERSION}\0" + "20260924 12:00:00 US/Eastern\0"
            conn.sendall(struct.pack("!I", len(hello)) + hello.encode())
            (n,) = struct.unpack("!I", self._recv_exact(conn, 4))       # first message: startApi
            first = self._recv_exact(conn, n)
            msg_id = int.from_bytes(first[:4], "big")
            if (msg_id - PB if msg_id > PB else msg_id) == OUT.START_API:
                req = StartApiRequest_pb2.StartApiRequest()
                req.ParseFromString(first[4:])
                client_id = req.clientId
                with self._lock:
                    self.start_api_client_ids.append(client_id)
                    in_use = client_id in self._client_ids
                    if not in_use:
                        self._client_ids.add(client_id)
                if in_use:
                    self.rejected_client_ids.append(client_id)
                    client_id = None
                    self._send_on(conn, IN.ERR_MSG, ErrorMessage_pb2.ErrorMessage(
                        id=-1, errorTime=int(time.time() * 1000), errorCode=326,
                        errorMsg="Unable to connect as the client id is already in use. Retry with a unique client id."))
                    time.sleep(0.05)
                    return                                               # TWS then closes the socket
            self.connections += 1
            self.conn = conn
            self._session(conn, first)
        except (OSError, ConnectionError, AssertionError):
            pass
        finally:
            if client_id is not None:
                with self._lock:
                    self._client_ids.discard(client_id)
            try:
                conn.close()
            except OSError:
                pass
            if self.conn is conn:
                self.conn = None

    def _recv_exact(self, conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client closed")
            buf += chunk
        return buf

    def _session(self, conn: socket.socket, first: bytes) -> None:
        payload = first
        while not self._stop:
            msg_id = int.from_bytes(payload[:4], "big")
            with self._lock:
                self.received.append((msg_id, payload[4:]))
            if self.auto:
                self._respond(msg_id, payload[4:])
            (n,) = struct.unpack("!I", self._recv_exact(conn, 4))
            payload = self._recv_exact(conn, n)

    # ------------------------------------------------------------------ auto-responses
    def _note(self, method: str, req_id: int) -> None:
        with self._lock:
            self.requests.setdefault(method, []).append(req_id)

    def _respond(self, msg_id: int, body: bytes) -> None:
        base = msg_id - PB if msg_id > PB else msg_id
        if base == OUT.START_API:
            if self.send_next_valid_id:
                self.next_valid_id(1)
        elif base == OUT.REQ_CURRENT_TIME:
            self.current_time(int(time.time()))
        elif base == OUT.REQ_CONTRACT_DATA:
            r = ContractDataRequest_pb2.ContractDataRequest()
            r.ParseFromString(body)
            self._note("reqContractDetails", r.reqId)
            self.contract_data(r.reqId)
            self.send(IN.CONTRACT_DATA_END, ContractDataEnd_pb2.ContractDataEnd(reqId=r.reqId))
        elif base == OUT.REQ_MARKET_RULE:
            r = MarketRuleRequest_pb2.MarketRuleRequest()
            r.ParseFromString(body)
            m = MarketRule_pb2.MarketRule(marketRuleId=r.marketRuleId)
            pi = m.priceIncrements.add()
            pi.lowEdge = 0.0
            pi.increment = 0.25
            self.send(IN.MARKET_RULE, m)
        elif base == OUT.REQ_MKT_DEPTH:
            r = MarketDepthRequest_pb2.MarketDepthRequest()
            r.ParseFromString(body)
            self._note("reqMktDepth", r.reqId)
            self.seed_depth(r.reqId)
        elif base == OUT.REQ_TICK_BY_TICK_DATA:
            r = TickByTickRequest_pb2.TickByTickRequest()
            r.ParseFromString(body)
            self._note("reqTickByTickData:" + r.tickType, r.reqId)
            self.tbt_types[r.reqId] = r.tickType
            if r.tickType == "BidAsk":
                self.bid_ask(r.reqId, self.base, self.base + 0.25)
            else:
                self.trade(r.reqId, self.base, 1)
        elif base == OUT.REQ_MKT_DATA:
            r = MarketDataRequest_pb2.MarketDataRequest()
            r.ParseFromString(body)
            self._note("reqMktData", r.reqId)
            if self.conflict_mode:
                self.error(-1, 10197, "No market data during competing live session")
                return
            self.market_data_type(r.reqId, 1)
            self.tick_price(r.reqId, 1, self.base)
            self.tick_price(r.reqId, 2, self.base + 0.25)

    # ------------------------------------------------------------------ message builders
    def send(self, in_msg_id: int, proto) -> None:
        body = (in_msg_id + PB).to_bytes(4, "big") + proto.SerializeToString()
        conn = self.conn
        if conn is not None:
            with self._send_lock:
                conn.sendall(struct.pack("!I", len(body)) + body)

    def _send_on(self, conn: socket.socket, in_msg_id: int, proto) -> None:
        body = (in_msg_id + PB).to_bytes(4, "big") + proto.SerializeToString()
        with self._send_lock:
            conn.sendall(struct.pack("!I", len(body)) + body)

    def next_valid_id(self, oid: int) -> None:
        self.send(IN.NEXT_VALID_ID, NextValidId_pb2.NextValidId(orderId=oid))

    def current_time(self, t: int) -> None:
        self.send(IN.CURRENT_TIME, CurrentTime_pb2.CurrentTime(currentTime=t))

    def error(self, req_id: int, code: int, msg: str = "") -> None:
        self.send(IN.ERR_MSG, ErrorMessage_pb2.ErrorMessage(id=req_id, errorTime=int(time.time() * 1000),
                                                            errorCode=code, errorMsg=msg))

    def contract_data(self, req_id: int) -> None:
        m = ContractData_pb2.ContractData(reqId=req_id)
        c = m.contract
        c.conId = 770561201
        c.symbol = "MNQ"
        c.secType = "FUT"
        c.lastTradeDateOrContractMonth = "20261218"
        c.multiplier = 2.0
        c.exchange = "CME"
        c.currency = "USD"
        c.localSymbol = "MNQZ6"
        c.tradingClass = "MNQ"
        d = m.contractDetails
        d.marketName = "MNQ"
        d.minTick = "0.25"
        d.validExchanges = "CME,QBALGO"
        d.marketRuleIds = "67,67"
        d.timeZoneId = "US/Central"
        d.tradingHours = "20260923:1700-20260924:1600"
        d.liquidHours = "20260924:0830-20260924:1500"
        self.send(IN.CONTRACT_DATA, m)

    def depth(self, req_id: int, position: int, operation: int, side: int, price: float, size: int) -> None:
        m = MarketDepth_pb2.MarketDepth(reqId=req_id)
        d = m.marketDepthData
        d.position = position
        d.operation = operation
        d.side = side
        d.price = price
        d.size = str(size)
        self.send(IN.MARKET_DEPTH, m)

    def seed_depth(self, req_id: int, rows: int = 5) -> None:
        for i in range(rows):
            self.depth(req_id, i, 0, 1, self.base - 0.25 * i, 10 + i)
            self.depth(req_id, i, 0, 0, self.base + 0.25 * (i + 1), 10 + i)

    def bid_ask(self, req_id: int, bid: float, ask: float, bs: int = 5, asz: int = 5) -> None:
        m = TickByTickData_pb2.TickByTickData(reqId=req_id, tickType=3)
        t = m.historicalTickBidAsk
        t.time = int(time.time())
        t.priceBid = bid
        t.priceAsk = ask
        t.sizeBid = str(bs)
        t.sizeAsk = str(asz)
        self.send(IN.TICK_BY_TICK, m)

    def trade(self, req_id: int, price: float, size: int) -> None:
        m = TickByTickData_pb2.TickByTickData(reqId=req_id, tickType=2)
        t = m.historicalTickLast
        t.time = int(time.time())
        t.price = price
        t.size = str(size)
        t.exchange = "CME"
        self.send(IN.TICK_BY_TICK, m)

    def market_data_type(self, req_id: int, t: int) -> None:
        self.send(IN.MARKET_DATA_TYPE, MarketDataType_pb2.MarketDataType(reqId=req_id, marketDataType=t))

    def tick_price(self, req_id: int, tick_type: int, price: float) -> None:
        self.send(IN.TICK_PRICE, TickPrice_pb2.TickPrice(reqId=req_id, tickType=tick_type, price=price,
                                                         size="1", attrMask=0))

    # ------------------------------------------------------------------ control
    def drop_connection(self) -> None:
        conn = self.conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def received_base_ids(self) -> list[int]:
        with self._lock:
            return [m - PB if m > PB else m for m, _ in self.received]

    def close(self) -> None:
        self._stop = True
        self.drop_connection()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(2)
