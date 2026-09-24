# Hermès — Phase C Architecture (Market Engine / Order-Flow Intelligence)

Status: **APPROVED** (architecture review + amendments of 2026-09-24)
Scope: market intelligence only. **No order execution. TWS API stays Read-Only.**

This document is the reference design for Phase C. When code and this document
disagree, one of them is a bug: fix the code or amend this document explicitly.

---

## 0. Decision log (approved amendments)

| # | Decision |
|---|----------|
| 1 | Tick-by-tick `BidAsk` is the **primary BBO source**. Top-of-book from market depth is a **cross-check**. Subscription capacity is never assumed: if IBKR rejects the extra tick-by-tick subscription, Hermès fails safe (book can never become `VALID` while `require_bbo_confirmation = true`). |
| 2 | **MNQ only** for Phase C implementation/validation. `instrument_id` / multi-instrument support exists in every event and state object from day one. NQ is added later as a contextual stream. |
| 3 | Depth rows default **10, configurable**. Book validity uses a **configurable minimum depth** plus: both sides present, strictly sorted prices, non-stale updates, non-persistent crossed state, and top-of-book agreement with tick-by-tick BidAsk within a synchronization tolerance. It does **not** require all rows populated. |
| 4 | **Single writer / deterministic engine.** Phase C uses the **official** ibapi `EReader` + `EClient.run()` dispatch. The thread executing EWrapper callbacks is the **sole MarketEngine writer**. We do **not** replace `run()` or subclass/modify `EReader`. Timestamp at callback entry. Replace ibapi internals only if measurements prove the official path is a material bottleneck. |
| 5 | Pipeline is explicitly `RawIbkrEvent → Normalizer → MarketEvent → MarketEngine`. The **authoritative recording / replay source is the raw stream** (`RawEvent`: IBKR callbacks + local control events). Normalized events may be recorded for diagnostics only. |
| 6 | The recorder **never blocks** the engine. On overflow: live continues, a `RecordingGap` is recorded when possible, the recording is marked **not replay-complete from that sequence**, telemetry increments, and live/replay equivalence is not claimed beyond that point. |
| 7 | Integer price units internally, via a **`PriceGrid`** abstraction built from `ContractDetails.minTick` **and** `marketRuleIds` / `reqMarketRule` (not minTick alone). MNQ resolves to the 0.25 CME grid. |
| 8 | Error **10197** is treated per current IBKR semantics as a **market-data session conflict** that **hard-blocks market validity while it exists**. No assumption about what triggers it. |
| 9 | `tradingHours` / `liquidHours` are **exchange session context only**. User-authorized entry hours are a separate, later **`UserTradingWindow`** used by strategy/risk gating. |
| 10 | No IBC / third-party IB Gateway automation dependency in Phase C. Evaluated at deployment time. |
| 11 | TWS Read-Only stays enabled. **No order execution path exists in Phase C.** `ReadOnlyClient` guard + tests blocking `placeOrder` / `cancelOrder` / `exerciseOptions` / etc. are approved. |

---

## 1. Principles (priority order)

1. Correctness → 2. Safety → 3. Determinism → 4. Observability → 5. Testability → 6. Low latency → 7. Maintainability → 8. Measured optimization.

Hard rules:

- The critical path is `IBKR → deterministic Python engine → (later) risk/position management`.
  It never waits on OpenAI/Sol, Slack, n8n, HTTP, databases, or chart rendering.
- No blocking I/O, sleeps, network calls, or synchronous DB/log-file writes inside IBKR callbacks.
- The engine never reads the wall clock and never uses randomness. All time enters as event fields.
- Every state object that strategy code may consume carries an explicit **quality state**.
  "Unknown" is a first-class value (book state, aggressor side, bar flags).
- Displayed liquidity is evidence, never a guarantee of execution.

---

## 2. Threading model

```
          TWS (127.0.0.1:7496, Read-Only)
                     │ socket
      ┌──────────────▼───────────────┐
T1    │ ibapi EReader (official)     │  reads socket → client.msg_queue
      └──────────────┬───────────────┘
      ┌──────────────▼───────────────────────────────────────────────┐
T2    │ ibapi EClient.run() (official) — "ibkr-dispatch" thread       │
      │  EWrapper callback entry: stamp mono_ns + wall_ns             │
      │   → IbkrAdapter builds RawIbkrEvent (seq)                     │
      │   → (timer due?) emit RawTimerTick first                      │
      │   → Recorder.submit(raw)          [non-blocking]              │
      │   → Normalizer → MarketEvent(s)                               │
      │   → MarketEngine.on_event()       [SOLE WRITER]               │
      │   → in-thread consumers (later strategy/risk, time-budgeted)  │
      │   → SnapshotPublisher (atomic reference swap)                 │
      └──────────────┬───────────────────────────────────────────────┘
T3    Recorder thread: drains bounded ring → batch encode → append file
T4    Heartbeat/Watchdog thread: reqCurrentTime via RequestGateway,
      samples msg_queue.qsize(), checks dispatch-thread liveness counter
Later Sidecar process (Slack / Sol / charts / DB) via local IPC
```

Rules:

- **T2 is the only thread that mutates market state.** No locks on engine state.
- Other threads read only the latest immutable `MarketSnapshot` reference (CPython reference assignment is atomic).
- **Timer ticks without replacing `run()`:** at every callback entry the adapter checks whether a timer
  interval has elapsed and, if so, emits `RawTimerTick` event(s) *before* the callback's own event.
  The heartbeat thread issues `reqCurrentTime()` every `heartbeat_interval_ms`; its `currentTime`
  response guarantees callbacks (and therefore ticks) even in silent markets, and doubles as the
  TWS-liveness heartbeat. Timer ticks are **recorded raw events**, so replay reproduces them exactly.
- **All outbound requests** go through one `RequestGateway` (lock-serialized, allowlisted). ibapi's
  `Connection.sendMsg` is lock-protected, but we do not rely on the rest of `EClient` being thread-safe.
- **Backlog indicator:** `client.msg_queue.qsize()` (read-only observation of a public attribute) sampled
  by the watchdog and at callback entry every N events. Sustained growth ⇒ engine too slow ⇒ book marked
  `STALE` + resubscribe (never silently drop depth events).
- Logging from T2 uses `logging.handlers.QueueHandler` → `QueueListener` (no synchronous file I/O on T2).
- CPU-heavy non-critical work (charts, Sol payload building, DB) lives in a **separate process** later,
  because threads in the same process compete for the GIL.
- Queue-hop cost is **not assumed**: if a queue between dispatch and engine is ever proposed, it must be
  justified by measurements from the instrumentation below.

---

## 3. Event pipeline

```
IBKR callback ──► RawIbkrEvent ──► Normalizer ──► MarketEvent ──► MarketEngine
local control ──► RawLocalEvent ─┘   (pure, deterministic, versioned)
                     │
                     └──► Recorder (authoritative raw stream)
```

### 3.1 Raw events (`hermes/ibkr/raw_events.py`)

Callback-level information **before semantic normalization**: IBKR field semantics, IBKR codes
(side 0/1, operation 0/1/2), float prices as received, `Decimal` sizes as received.
The module imports nothing from `ibapi`, so replay works without the TWS API installed.

Common header (`RawEvent`): `seq` (global, monotonic, assigned by the adapter), `recv_mono_ns`
(`time.perf_counter_ns()` at callback entry), `recv_wall_ns` (`time.time_ns()` at callback entry).

| Raw type | Source |
|---|---|
| `RawMarketDepth` | `updateMktDepth` / `updateMktDepthL2` (`is_l2`, `market_maker`, `is_smart_depth` preserved) |
| `RawTickByTickAllLast` | `tickByTickAllLast` (incl. `past_limit`, `unreported`, exchange, special conditions) |
| `RawTickByTickBidAsk` | `tickByTickBidAsk` (incl. `bid_past_low`, `ask_past_high`) |
| `RawContractDetails` / `RawContractDetailsEnd` | contract resolution (selected fields incl. `min_tick`, `market_rule_ids`, `valid_exchanges`, hours, tz) |
| `RawMarketRule` | `marketRule` price increments |
| `RawError` | `error` (reqId, errorTime, code, message, advanced JSON) |
| `RawMarketDataType`, `RawCurrentTime`, `RawNextValidId`, `RawConnectionClosed`, `RawConnectAck` | session/control callbacks |
| `RawTimerTick` *(local)* | adapter timer at callback entry |
| `RawRequestIssued` *(local)* | every subscription/cancel we send (method, reqId, params) |
| `RawRecordingGap` *(local)* | recorder overflow marker (see §7) |
| `RawSessionMarker` *(local)* | start/stop of a recording session, config + versions |

### 3.2 Normalizer (C3)

Pure function object: `RawEvent → tuple[MarketEvent, ...]` (0..n). Responsibilities:
IBKR code mapping (side `0 = ASK`, `1 = BID`; op `0 = insert`, `1 = update`, `2 = delete`),
`Decimal → int` sizes (non-integral ⇒ anomaly), `float → grid units` via `PriceGrid`
(off-grid ⇒ anomaly event, not a crash), sentinel handling (e.g. empty-side prices),
error-code classification (§9), reqId → (instrument, stream) routing.
The normalizer carries a `NORMALIZER_VERSION`; recordings store it.

### 3.3 Market events (`hermes/market/events.py`)

IBKR-independent, frozen, slotted, keyword-only dataclasses. Header: `seq` (source raw seq),
`sub` (index within that raw event), `instrument_id`, `recv_mono_ns`, `recv_wall_ns`.

| Event | Payload |
|---|---|
| `DepthRowEvent` | `side: BookSide`, `op: DepthOp`, `position`, `price_units`, `size` |
| `DepthResetEvent` | `reason: ResetReason` (`IBKR_317`, `RESUBSCRIBE`, `DISCONNECT`, `MANUAL`) |
| `TradeEvent` | `price_units`, `size`, `exch_ts_s`, `exchange`, `special_conditions`, `past_limit`, `unreported` |
| `BboEvent` | `bid_units`, `ask_units`, `bid_size`, `ask_size`, `exch_ts_s` (from tick-by-tick BidAsk) |
| `StreamStatusEvent` | `stream: Stream`, `status: StreamStatus`, `code`, `detail` |
| `ConnectionEvent` | `state: ConnectionState`, `code` |
| `ClockTickEvent` | (header only) |
| `InstrumentDefinitionEvent` | `con_id`, `symbol`, `local_symbol`, `expiry`, `multiplier`, `price_grid`, `time_zone`, `trading_hours`, `liquid_hours` |
| `DataAnomalyEvent` | `kind`, `detail` (off-grid price, non-integral size, …) |

---

## 4. Timestamps and latency instrumentation

| Stamp | Where | Meaning |
|---|---|---|
| `recv_mono_ns`, `recv_wall_ns` | callback entry (T2) | earliest point available without modifying ibapi |
| `exch_ts_s` | tick-by-tick trades / BidAsk | **1-second resolution** from IBKR; depth has **no** exchange timestamp |
| `proc_start_ns`, `proc_end_ns` | engine | processing latency |
| `publish_ns` | snapshot publisher | snapshot publication latency (`publish_ns − recv_mono_ns`) |

- Sub-second ordering comes **only** from arrival order (`seq`) and `recv_mono_ns`.
  Velocity/burst metrics use `recv_mono_ns`.
- Latency is aggregated in fixed-bucket histograms (p50/p99/max logged every 10 s), never per-event logs.
- Clock-skew monitor: `recv_wall − exch_ts` on trades; persistent excess ⇒ data-delay/clock issue ⇒ entry gate.
- Not measurable without modifying ibapi (deferred, per decision 4): socket-read time and time spent in `msg_queue`.

---

## 5. PriceGrid (`hermes/market/pricegrid.py`)

- Built from `min_tick` **and** the market rule's `(low_edge, increment)` bands (`reqMarketRule`).
  The market rule for the trading exchange is selected by position: `validExchanges[i] ↔ marketRuleIds[i]`
  (`hermes/ibkr/market_rules.py`).
- Internal **unit** = GCD of all increments and `min_tick` (exact `Decimal` arithmetic). Every legal price
  is an integer number of units. For MNQ: unit = step = 0.25 ⇒ units == ticks.
- `to_units(price)` rejects non-finite and off-grid prices (tolerance 1e-6 unit) with `OffGridPriceError`.
- `is_legal(units)`, `step_at(units)`, `next_up/next_down`, `ticks_between(a, b)` implement band rules
  (legal = multiple of the band's increment; prices below the first low edge use the first band).
- `min_tick_matches_rule` flags a `minTick` inconsistent with the rule (logged, not fatal).
- Everything downstream (book, tape, bars, metrics) works in integer units. Conversion back to float/Decimal
  happens only at presentation boundaries.

---

## 6. Order book (`hermes/market/orderbook.py`)

### 6.1 Row-position semantics

IBKR depth is **row-indexed**, not price-indexed. Each side is a list of rows `(price_units, size)`,
best first, at most `depth_rows` long.

| Op | Valid when | Effect |
|---|---|---|
| INSERT | `0 ≤ pos ≤ len` and `pos < depth_rows` | insert at `pos`, rows below shift down, truncate to `depth_rows` |
| UPDATE | `0 ≤ pos < len` | replace price **and** size at `pos` (price may change) |
| DELETE | `0 ≤ pos < len` | remove row, rows below shift up |

Any other position, or a negative size, is a **structural violation**: the row array is no longer
knowable ⇒ book goes `STALE` immediately, rows are cleared, further row ops are ignored (counted) until a
reset, and `needs_resync` is raised for the engine (C3) to cancel/re-request depth (rate-limited).

**Liquidity changes** are computed by diffing the side's price→size map before/after each op — never from
the op type. Each `LevelChange` carries `at_window_edge` when a level left or entered via the last visible
row of a full side (truncation / tail refill), because that is visibility, not add/cancel.

### 6.2 Quality state machine

```
 EMPTY ──first row op / reset──► BUILDING ──all conditions OK for settle_ms──► VALID
                                   ▲                                          │ condition fails
                     reset(reason) │                                          ▼
 STALE ◄──structural violation / escalation / invalidate()────────────── SUSPECT
   │                                                                          │
   └──── reset(reason) ─► BUILDING                OK for settle_ms ◄──────────┘
```

Validity conditions (all required, evaluated on every book/BBO event and on clock ticks):

| Condition | Issue when failing | Grace | Escalates to STALE |
|---|---|---|---|
| each side has ≥ `min_valid_rows` (implies both sides present) | `INSUFFICIENT_DEPTH` | – | no |
| both sides strictly sorted (bids ↓, asks ↑) | `UNSORTED` | `transient_grace_ms` | after `escalate_after_ms` |
| not crossed/locked (`best_bid < best_ask`) | `CROSSED` | `transient_grace_ms` | after `escalate_after_ms` |
| last depth update age ≤ `max_update_age_ms` | `UPDATE_AGE` | – | no (session-aware handling in C9) |
| BBO reference available (if `require_bbo_confirmation`) | `BBO_UNAVAILABLE` | – | no |
| `|book top − BBO| ≤ bbo_tolerance_ticks` on both sides | `BBO_MISMATCH` | `bbo_mismatch_grace_ms` | after `escalate_after_ms` |

- The BBO's *absolute age* is intentionally **not** a validity condition: tick-by-tick BidAsk only updates on
  change, so a quiet but correct BBO can be old. The synchronization/age tolerance is expressed as
  `bbo_mismatch_grace_ms` (how long book and BBO may disagree) plus `max_update_age_ms` on the depth stream.
- Grace absorbs the inherent race between the depth stream and the BidAsk stream (separate IBKR streams,
  no common sub-second timestamp). A condition's timer restarts whenever it is satisfied again.
- **Error 317**: `reset(IBKR_317)` clears both sides, increments `epoch`, clears `needs_resync`, state
  `BUILDING`. The book is `VALID` again only when every condition holds continuously for `settle_ms`.
- `invalidate(reason)` (disconnect, 1101, 10197, backlog overflow, …) ⇒ `STALE` + `needs_resync`.
- `epoch` increments on every reset so consumers can detect discontinuities.
- All time comes from event `recv_mono_ns` values (deterministic under replay).

Defaults (`config/hermes.toml` → `[book]`): `depth_rows = 10`, `min_valid_rows = 5`, `settle_ms = 500`,
`max_update_age_ms = 5000`, `transient_grace_ms = 250`, `bbo_tolerance_ticks = 0`,
`bbo_mismatch_grace_ms = 1000`, `escalate_after_ms = 3000`, `require_bbo_confirmation = true`.

**Limits of the data:** IBKR provides aggregated price-level depth (MBP, ~10 levels), not per-order data
(MBO), and updates may be conflated. No queue-position inference; "wall persistence" means a price level
persisted, not a specific order; no metric may assume every exchange book event is observed.

---

## 7. Recording and replay (C3 / C6)

- **Authoritative stream:** every `RawEvent` (IBKR callbacks + local control events) in `seq` order.
- Format: length-prefixed msgpack records, batched appends, file rotation per session/hour,
  zstd compression at rotation (not inline). Header: schema version, normalizer version, config,
  contract details, git hash, ibapi version, Python version. Reader tolerates a truncated last record.
- **Non-blocking submission:** `Recorder.submit(ev) -> bool` appends to a bounded ring with one slot of
  headroom reserved for gap markers. When full, the event is dropped and the gap range
  (`first_seq`, `last_seq`, `count`) accumulates. On the next accepted submission, a
  `RawRecordingGap` is enqueued first.
- On any gap: `recording.replay_complete = False` from `first_seq` (exposed in health and written to the
  file index), telemetry counter incremented, live/replay equivalence assertions stop at that seq.
- Normalized `MarketEvent`s may be recorded to a separate diagnostic stream; never used as replay truth.
- **Replay:** `ReplaySource` reads raw records → same `Normalizer` → same `MarketEngine`, time taken from the
  recorded stamps (`ManualClock`). Modes: as-fast-as-possible (tests) and paced (visual debugging).
- **Equivalence check:** live runs record a hash of each published snapshot; replay must reproduce the identical
  hash sequence up to the first recording gap.

---

## 8. Tape, classification, bars, metrics, market state (C4–C8, summary)

- **Tape:** bounded ring (count **and** time bound). Trades classified BUY / SELL / UNKNOWN with
  `method` (QUOTE, QUOTE_HISTORY, TICK_RULE, NONE) and confidence, using the tick-by-tick BBO state just
  before the trade plus a short BBO history to detect "quote updated before its trade arrived".
  Delta always reports buy / sell / unknown volume separately.
- **Fill vs cancel attribution:** size decreases at a price are held pending for ±W ms and matched against
  trades at that price in either arrival order ⇒ EXECUTED / CANCELED / AMBIGUOUS.
- **Bars (30 s / 1 m / 5 m):** UTC-epoch aligned; trades assigned by exchange timestamp; bars closed by
  `ClockTickEvent` at boundary + grace; late trades counted/flagged, closed bars never rewritten;
  empty intervals emit flat zero-volume bars; `flags` (GAP, BOOK_STALE, PARTIAL, LATE_TRADES);
  `ext: Mapping[str, float]` for attached metrics.
- **Metrics:** incremental O(1) rolling windows; imbalance (L1/L3/L5/L10, weighted), OFI, microprice,
  signed flow, velocity, bursts, level episodes (persistence, replenishment, executed vs canceled,
  relocation), absorption, icebergs, sweeps, spread behavior, price impact, exhaustion inputs.
  "Large" is always relative to rolling distributions. Measurements only — no trading rules.
- **MarketSnapshot:** immutable; built on cadence (default 100 ms), on bar close, and on demand; carries
  `seq`, book snapshot + state/issues/epoch, BBO, recent classified trades, bars, session levels, features
  with validity, per-stream health, recording completeness, and `data_ok_for_entry`.

---

## 9. Health, errors, sessions

| Code(s) | Handling |
|---|---|
| 317 | depth reset ⇒ `reset(IBKR_317)` |
| 1100 | connectivity lost ⇒ all streams `STALE`, book `invalidate` |
| 1101 | restored, data lost ⇒ resubscribe everything, rebuild |
| 1102 | restored, data maintained ⇒ still re-verify book (BBO cross-check) |
| 2103 / 2105 / 2157 ; 2104 / 2106 / 2158 | farm broken ⇒ affected streams degraded ; farm OK (informational) |
| 2110 | TWS ↔ IBKR server connectivity broken ⇒ streams degraded |
| 309 | max depth subscriptions exceeded ⇒ depth `UNAVAILABLE`, fail safe |
| 354 / 10090 | not subscribed / partially subscribed ⇒ stream `UNAVAILABLE` |
| 10167, `marketDataType` ∈ {2,3,4} | delayed/frozen data ⇒ **hard block** (never treated as live) |
| **10197** | **market-data session conflict ⇒ hard block of market validity while it exists**; cleared only by a successful resubscription followed by fresh data (exact clearing semantics to be confirmed with live observation) |
| tick-by-tick subscription rejected | BBO `UNAVAILABLE` ⇒ book cannot be `VALID` (decision 1) |

- **Exchange session context** (`tradingHours`, `liquidHours`, `timeZoneId`, CME daily maintenance halt,
  holidays) distinguishes "market closed/quiet" from "disconnected". It is **not** authorization to trade.
- **`UserTradingWindow`** (later) is the only source of authorized entry hours; managing/closing an existing
  position must remain operational outside it.
- Watchdog distinguishes: heartbeat OK + no data (quiet/closed/halt) vs no heartbeat (disconnected).
- Contract roll: warn when inside the configured roll window; contract resolution asserts exactly one match.

---

## 10. Safety: read-only enforcement (C1)

Defense in depth, all active in Phase C:

1. **TWS Read-Only API** setting stays enabled.
2. **`ReadOnlyClient`** (`hermes/ibkr/readonly.py`) — the only permitted `EClient`:
   - **Method allowlist (default deny):** every callable on `EClient` not explicitly allowlisted is replaced
     by a function raising `OrderApiForbiddenError`. New methods in future ibapi versions are therefore
     blocked automatically.
   - **Explicit forbidden set** (must never be allowlisted): `placeOrder`, `cancelOrder`, `reqGlobalCancel`,
     `exerciseOptions`, `reqAutoOpenOrders`, `replaceFA`, `updateConfigProtoBuf`, order-parameter validators,
     and all `…ProtoBuf` variants.
   - **Message-id guard:** `sendMsg` / `sendMsgProtoBuf` refuse forbidden outgoing message ids
     (`PLACE_ORDER`, `CANCEL_ORDER`, `REQ_GLOBAL_CANCEL`, `EXERCISE_OPTIONS`, `REQ_AUTO_OPEN_ORDERS`,
     `REPLACE_FA`, `UPDATE_CONFIG`), including the protobuf offset — catches unbound-base-class bypasses.
   - **Wire guard:** any connection assigned to `client.conn` is wrapped; every outbound frame is parsed and
     forbidden message ids are refused *before* bytes reach the socket. Unparseable frames are refused
     (fail closed).
   - **Violation latch:** ibapi wraps several request bodies in `except Exception` and reports failures via
     `EWrapper.error` instead of re-raising (observed on legacy/text server versions). Every blocked attempt is
     therefore also latched in `client.readonly_violations`; nothing is sent in any case. From C3 on, a
     non-empty latch is a critical safety event (engine/watchdog alert).
   - Known residual: code that deliberately calls the unwrapped socket (`client.conn.inner.sendMsg`) is not
     guarded by Python; the TWS Read-Only setting remains the final barrier.
3. **Static tests:** AST scan of `hermes/` fails on any use of forbidden method names, imports of
   `ibapi.order*`, or direct `EClient` subclassing/instantiation outside `readonly.py`
   (legacy Phase B diagnostics are an explicit, temporary exception list).
4. **Config:** `[safety] orders_enabled` must be `false` and `[ibkr] read_only` must be `true`, or config
   loading fails.
5. **No `hermes/execution` package** exists in Phase C (tested).
6. ibapi version is pinned (**10.45.1**) because guards rely on its method/message layout; a version change
   fails a test until the guard is re-audited.

---

## 11. Future execution provisions (not implemented)

Separate execution connection and `clientId`; broker-native protective stop attached to entry; broker is
source of truth for positions/orders (startup reconciliation via positions/open orders/executions);
entry gating on `data_ok_for_entry`; single-use, TTL-bound approval tokens; idempotent `orderRef`;
partial-fill / reject handling; stops never widened once risk is reduced; standalone kill-switch script
working without the core; position management continues outside entry hours and on stale data.

---

## 12. Deployment

Development: MacBook Pro, Python 3.11, `~/hermes-trading/.venv`. Target: Mac Mini Intel (x86_64).
No Apple-Silicon-only dependencies; runtime deps limited to ibapi (TWS distribution, pinned) and later
`msgpack` / `zstandard`. Dev deps: `pytest`, `hypothesis`. No IBC/third-party gateway automation in Phase C.
Operational notes: prevent sleep/App Nap during sessions; Docker compatibility is a goal, not a Phase C task.

---

## 13. Package layout

```
hermes/
  config.py                 TOML → frozen, validated dataclasses (stdlib tomllib)
  core/clock.py             Clock protocol, SystemClock, ManualClock
  ibkr/raw_events.py        RawEvent hierarchy (no ibapi import)
  ibkr/codes.py             IBKR side/op code maps, forbidden message ids
  ibkr/market_rules.py      marketRuleIds ↔ validExchanges selection
  ibkr/readonly.py          ReadOnlyClient + guards
  ibkr/adapter.py           (C3) callbacks → RawEvents, timer ticks
  ibkr/normalizer.py        (C3) RawEvent → MarketEvent
  ibkr/gateway.py           (C3) serialized allowlisted requests
  market/events.py          MarketEvent hierarchy + enums
  market/pricegrid.py       PriceGrid
  market/orderbook.py       OrderBook (rows + quality state machine)
  market/tape.py, classify.py, bars.py, session.py, health.py, engine.py, snapshot.py, metrics/   (C4+)
  storage/recorder.py, replay.py, codec.py                                                       (C3/C6)
  safety/watchdog.py                                                                             (C9)
config/hermes.toml
docs/ARCHITECTURE_PHASE_C.md
tests/unit, tests/property, tests/safety, (later) tests/scenarios, tests/replay, tests/live
```

---

## 14. Test plan (Phase C reliability gate)

- **C1:** config validation (read-only enforced, unknown keys, types); clocks; PriceGrid (MNQ, multi-band,
  off-grid, non-finite, legality, stepping, rule selection) incl. property round-trips; event immutability;
  raw events importable without ibapi; ReadOnlyClient (every forbidden method raises without sending bytes,
  default deny, unbound-bypass blocked at message-id layer, raw forbidden frames blocked at wire layer,
  allowed requests encode and pass for legacy and protobuf server versions, allowlist call-graph closure,
  ibapi version pin); static AST scan.
- **C2:** row semantics (insert/update/delete incl. shifting, truncation, price-changing update),
  structural violations ⇒ STALE, STALE ignores ops, reset/epoch, 317 rebuild criteria, min-depth validity
  without full rows, crossed/unsorted grace and escalation, BBO unavailable/mismatch/tolerance/escalation,
  update age, analytics (best/spread/mid/levels/totals), level-change diff incl. window-edge flags,
  determinism; property tests against a reference model with (a) arbitrary valid ops, (b) a well-formed
  exchange-like feed derived from a true price-level book, (c) arbitrary garbage ops (never raises, fails safe).
- **C3–C9:** adapter mapping and error routing, recorder backpressure and gap semantics, tape/classifier cases,
  bar boundaries and multi-timeframe consistency, metric scenarios (flash wall vs absorbing wall, iceberg,
  sweep, exhaustion), snapshot consistency, replay determinism and live/replay equivalence, throughput/latency
  benchmarks, memory soak, live checks (DOM comparison, disconnect recovery, historical-bar comparison).

---

## 15. Milestones

C1 skeleton/config/clock/events/PriceGrid/ReadOnlyClient · C2 order book · C3 adapter/normalizer/gateway/recorder
(start recording live sessions) · C4 BBO/tape/classifier · C5 bars/session · C6 replay + equivalence ·
C7 metrics L1 · C8 metrics L2 (level episodes, attribution, absorption…) · C9 health/watchdog/reconnect/soak.
