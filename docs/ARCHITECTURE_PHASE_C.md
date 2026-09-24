# Hermès — Phase C Architecture (Market Engine / Order-Flow Intelligence)

Status: **APPROVED** — architecture review + amendments (2026-09-24), C3 decisions and amendments A–F.
Implementation: C1 ✅ C2 ✅ C3 ✅ (pending live smoke test on the Mac) · C4+ not started.
Scope: market intelligence only. **No order execution. TWS API stays Read-Only.**

This document is the reference design for Phase C. When code and this document
disagree, one of them is a bug: fix the code or amend this document explicitly.

---

## 0. Decision log

### 0.1 Architecture amendments (approved with C1/C2)

| # | Decision |
|---|----------|
| 1 | Tick-by-tick `BidAsk` is the **primary BBO source**; depth top-of-book is a **cross-check**. Subscription capacity is never assumed: if IBKR rejects the extra tick-by-tick subscription, Hermès fails safe (book can never become `VALID` while `require_bbo_confirmation = true`). |
| 2 | **MNQ only** in Phase C. `instrument_id` exists in every event/state object from day one; NQ is added later as a contextual stream. |
| 3 | Depth rows default **10, configurable**. Validity uses a **configurable minimum depth** plus sorted prices, non-persistent crossed state and BBO agreement within a synchronization tolerance. Full depth is **not** required. |
| 4 | **Single writer / deterministic engine** on the **official** ibapi `EReader` + `EClient.run()` path. The callback thread is the sole MarketEngine writer. No replacement/subclassing of ibapi internals unless measurements prove a bottleneck. |
| 5 | `RawIbkrEvent → Normalizer → MarketEvent → MarketEngine`. The **raw stream is the authoritative recording/replay source**. |
| 6 | The recorder **never blocks**. On overflow: live continues, a `RecordingGap` is recorded when possible, the recording is **not replay-complete from that seq**, telemetry increments; no equivalence claim beyond that point. |
| 7 | Integer price units via **`PriceGrid`** from `minTick` **and** `marketRuleIds`/`reqMarketRule`. |
| 8 | **10197** = market-data session conflict ⇒ **hard block** while it exists. No assumption about its trigger. |
| 9 | `tradingHours`/`liquidHours` are exchange context only; authorized entry hours = future **`UserTradingWindow`**. |
| 10 | No IBC / third-party gateway automation in Phase C. |
| 11 | TWS Read-Only stays enabled; **no order execution path** exists in Phase C. |

### 0.2 C3 decisions

| # | Decision |
|---|----------|
| D1 | **msgpack** (pinned `1.2.2`) is the recording codec; the format is explicitly **schema-versioned** and self-describing. |
| D2 | **reqMktData (L1) enabled by default** — used ONLY for `marketDataType`/LIVE confirmation and health cross-checks, never for aggressor classification or order-flow calculations. |
| D3 | 10197 recovery does **not** require a new trade print (quiet markets are legitimate). |
| D4 | 10197 retries are **automatic but bounded**: every 30 s, **max 3 consecutive attempts**; then relevant market data UNAVAILABLE, automatic resubscription stops, critical alert. Budget resets only on a meaningful connection/session change or explicit operator retry. No infinite loops. |
| D5 | Recordings default to **`~/hermes-data/recordings/`** (outside the repo; configurable). Logs default to `~/hermes-data/logs/`. |
| D6 | Depth: 10 rows default, configurable. |
| D7 | Thresholds (min depth 5, settle 500 ms, crossed/unsorted grace 250 ms, BBO mismatch grace 1 s, stale escalation 3 s) are **initial defaults only** — configuration parameters to be calibrated from recorded MNQ sessions. |
| D8 | Phase B diagnostics are ported to `ReadOnlyClient` and live under `tools/phase_b/`. There is **no** direct-EClient exception anywhere. |

### 0.3 C3 amendments

| # | Amendment | Implementation |
|---|-----------|----------------|
| A | **Subscription generations.** Every (re)subscription uses a new reqId; callbacks of replaced reqIds are recorded raw but never mutate state. | Gateway allocates monotonically increasing, never-reused reqIds. Normalizer tracks the ACTIVE reqId per (instrument, stream) from recorded `RawRequestIssued`; old-reqId callbacks normalize to `DataAnomalyEvent(INACTIVE_REQ_ID)` only. The engine re-checks `event.generation` against the stream's active generation (second layer). Tested for depth resync, 317, BBO/AllLast/L1 replacement, reconnect, 10197 recovery (unit + end-to-end). |
| B | **Stream age is not a failure by itself.** | `max_update_age_ms = 0` (disabled) by default; ages are telemetry. Health combines connection state, farm state, subscription errors, marketDataType, book-vs-BBO consistency, recovery state. A trade far outside an old BBO increments `bbo_frozen_suspect` (evidence, not a verdict). |
| C | **Clock ticks are not a precise timer.** | `RawTimerTick` is emitted at callback entry when due; it records `due_mono_ns` (when due), `recv_mono_ns` (when emitted) and `coalesced` (intervals covered). A 1 s `reqCurrentTime` heartbeat guarantees callbacks while connected. Tighter bar-close timing is revisited in C5. No ibapi internals modified. |
| D | **True request timestamps.** | `RawRequestIssued.sent_mono_ns/sent_wall_ns` are captured inside the gateway immediately before the send; `seq`/`recv_*` are assigned later when sequenced on the dispatch thread. The event is queued BEFORE the send, so it always precedes its responses. |
| E | **Continuity-verified replay completeness.** | Gaps are file-level records; the raw `seq` space stays contiguous. `verify_session` / `tools/inspect_recording.py` recompute continuity from the records on disk: missing seqs WITHOUT a gap record (e.g. disk full so the gap record itself failed) are reported as UNDECLARED and the recording is never reported replay-complete. |
| F | **Request pacing is a safety guard.** | One conservative global token bucket (10 req/s, burst 20) plus optional endpoint-specific limits (`endpoint_limits`). Multi-request operations (full resubscription) check `can_send(n)` first so they are never half-sent. C3 request volume is very low (≈1 heartbeat/s + a handful of subscriptions). |

---

## 1. Principles (priority order)

1. Correctness → 2. Safety → 3. Determinism → 4. Observability → 5. Testability → 6. Low latency → 7. Maintainability → 8. Measured optimization.

- The critical path (`IBKR → deterministic engine → later risk/position management`) never waits on Sol, Slack, n8n, HTTP, databases or charts.
- No blocking I/O, sleeps, network calls or synchronous log writes inside IBKR callbacks.
- The engine never reads a clock and never uses randomness: all time enters as event fields.
- Every consumable state carries an explicit quality state; "unknown" is first-class.
- Displayed liquidity is evidence, never a guarantee of execution.

---

## 2. Threading model (as implemented in C3)

```
          TWS (127.0.0.1:7496, Read-Only)
                     │ socket
      ┌──────────────▼───────────────┐
T1    │ ibapi EReader (official)     │  socket → client.msg_queue
      └──────────────┬───────────────┘
      ┌──────────────▼────────────────────────────────────────────────┐
T2    │ ibapi EClient.run() (official) — "ibkr-dispatch"               │
      │ IbkrAdapter callback: perf_counter_ns + time_ns FIRST          │
      │  RawPipeline (lock; owner = current thread):                   │
      │   1 drain local events (gateway requests, controls)            │
      │   2 RawTimerTick if due                                        │
      │   3 RawEvent(seq) → Recorder.submit   [non-blocking]           │
      │   4 Normalizer → MarketEngine.on_event [owner-guarded]         │
      │   5 IbkrSession.after_event (in-thread consumer) → Gateway     │
      │   6 drain what the consumer posted; snapshot on cadence        │
      └──────────────┬────────────────────────────────────────────────┘
T3  recorder-writer   bounded deque → msgpack encode → append .hrec
T4  heartbeat         reqCurrentTime every 1 s; samples msg_queue depth; read-only violation latch
T5  telemetry         JSON report every 10 s (+ console line)
T6  logging listener  QueueHandler → files/console (no I/O on T2)
main                  Supervisor: connect (fresh ReadOnlyClient per attempt), reconnect with
                      bounded backoff, SIGINT/SIGTERM clean shutdown, SIGUSR1 operator retry
```

- `connectAck` and connect errors fire on the thread calling `connect()` (before/after T2 runs). Every entry takes the pipeline lock, and the engine's **owner guard** raises if anything writes it outside the pipeline's current owner — so the engine is always driven by exactly one thread.
- A callback never raises into ibapi (an exception would end `EClient.run()` and disconnect): failures are counted, logged and converted into a critical `internal_error` alert with books invalidated.
- `RequestGateway` is the only path to TWS requests (lock-serialized, allowlisted, generation reqIds, true send timestamps).
- Backlog indicators: ibapi `msg_queue.qsize()` (now/max), recorder backlog/high-watermark.

---

## 3. Event pipeline

### 3.1 Raw events (`hermes/ibkr/raw_events.py`, no ibapi import)

Header: `seq` (global, contiguous), `recv_mono_ns`, `recv_wall_ns` (callback entry / sequencing time).

| Raw type | Source |
|---|---|
| `RawMarketDepth` | `updateMktDepth` / `updateMktDepthL2` |
| `RawTickByTickAllLast`, `RawTickByTickBidAsk` | tick-by-tick |
| `RawTickPrice`, `RawTickSize`, `RawMarketDataType` | reqMktData (L1 cross-check) |
| `RawContractDetails`, `RawContractDetailsEnd`, `RawMarketRule` | contract resolution |
| `RawError`, `RawCurrentTime`, `RawNextValidId`, `RawConnectAck`, `RawConnectionClosed` | session |
| `RawTimerTick` *(local)* | `due_mono_ns`, `coalesced` (amendment C) |
| `RawRequestIssued` *(local)* | every gateway request, `sent_mono_ns/sent_wall_ns` (amendment D) |
| `RawRequestFailed` *(local)* | local send failure |
| `RawControl` *(local)* | supervisor/operator decisions: `connect_attempt`, `connect_failed`, `conflict_recovery_attempt`, `operator_retry`, `depth_resync_exhausted`, `contract_timeout`, `market_rule_timeout`, `readonly_violation`, … |
| `RawSessionMarker` *(local)* | reserved |

Recording gaps are **not** raw events (see §7).

### 3.2 Normalizer (`hermes/ibkr/normalizer.py`, `NORMALIZER_VERSION = 1`)

Pure, deterministic, never raises. IBKR code mapping (side 0 = ASK / 1 = BID; op 0/1/2),
Decimal → int sizes, float → grid units, generation routing (amendment A), error
classification (`hermes/ibkr/errors.py`), contract resolution + PriceGrid from the recorded
contract details and market rule (`hermes/ibkr/contracts.py`). Anomalies become
`DataAnomalyEvent`s handled fail-safe by the engine.

### 3.3 Market events (`hermes/market/events.py`)

Header: `seq`, `sub`, `instrument_id`, `recv_mono_ns`, `recv_wall_ns`, `generation`.
Types: `DepthRowEvent`, `DepthResetEvent`, `TradeEvent`, `BboEvent`, `L1TickEvent`,
`MarketDataTypeEvent`, `SubscriptionEvent`, `RequestFailedEvent`, `ErrorEvent`,
`ConnectionEvent`, `HeartbeatEvent`, `ClockTickEvent`, `ControlEvent`, `ContractResolvedEvent`,
`ContractFailedEvent`, `InstrumentDefinitionEvent`, `DataAnomalyEvent`.

---

## 4. Timestamps and latency instrumentation

| Measurement | Where | Telemetry key |
|---|---|---|
| callback processing latency | callback entry → end of pipeline processing | `callback_total` |
| core event processing | normalize + all engine events of one raw event | `core_total`, `normalize`, `engine_event` |
| snapshot publication latency | callback entry → snapshot published | `snapshot_publish` |
| recorder backlog / drops / gaps | recorder stats | `recorder.*` |
| stream age | now − last event of the active generation (telemetry only) | `instruments.*.streams.*.age_ms` |
| book state | state, issues, epoch, rows, resets, invalidations, violations, transitions | `instruments.*.book` |
| subscription state | status, generation, requests, last error | `instruments.*.streams` |
| ibapi backlog | `msg_queue.qsize()` now/max | `ibapi_msg_queue` |

Histograms: allocation-free log2 buckets (p50/p90/p99 are bucket upper bounds, max exact),
reported per window. Exchange timestamps from IBKR have **1 s resolution**; depth has none.

---

## 5. PriceGrid

Unit = exact GCD of all market-rule increments and `minTick`; legality per band; off-grid /
non-finite prices rejected; `min_tick_matches_rule` flagged. Market rule selected by position
(`validExchanges[i] ↔ marketRuleIds[i]`). MNQ ⇒ unit 0.25 = 1 tick.

---

## 6. Order book

Row-position semantics (insert/update/delete with shifting and truncation), structural
violations ⇒ STALE + `needs_resync`, liquidity changes from price→size diffs with
window-edge flags. Quality states EMPTY → BUILDING → VALID ⇄ SUSPECT, STALE.

| Condition | Issue | Grace | Escalates |
|---|---|---|---|
| each side ≥ `min_valid_rows` | `INSUFFICIENT_DEPTH` | – | no |
| strictly sorted | `UNSORTED` | `transient_grace_ms` | `escalate_after_ms` |
| not crossed/locked | `CROSSED` | `transient_grace_ms` | `escalate_after_ms` |
| depth update age ≤ `max_update_age_ms` | `UPDATE_AGE` | **disabled by default (0)** — amendment B | no |
| BBO reference available | `BBO_UNAVAILABLE` | – | no |
| \|top − BBO\| ≤ `bbo_tolerance_ticks` | `BBO_MISMATCH` | `bbo_mismatch_grace_ms` | `escalate_after_ms` |

Invalidation reasons: structural violation, persistent crossed/unsorted/BBO mismatch,
disconnect, data lost (1101, farm broken, 2110), session conflict (10197), data not live,
data anomaly, subscription failed, internal error, backlog, manual. Resubscription of depth
resets the book (new epoch) deterministically via the recorded request.

---

## 7. Recording and replay

- **Location:** `~/hermes-data/recordings/<YYYY-MM-DD>/<session_id>/part-NNNN.hrec`, rotated every `rotate_minutes`.
- **Format:** `MAGIC` + length-prefixed msgpack records: `HEADER` (schema version, type table, session/part, meta: versions, git commit, config, contract spec), `RAW` (type code + field values), `GAP`, `FOOTER`. `Decimal` via msgpack ext type; no pickle.
- **Submit** (dispatch thread): appends a reference to a bounded deque; one slot reserved for gap markers; never blocks.
- **Overflow:** event dropped, gap range accumulates and is queued ahead of the next accepted event (`reason=overflow`), `replay_complete=False`, `first_gap_seq` set, counters.
- **Write failure:** lost range recorded; the writer tries to persist a `GAP(reason=write_error)` in a new part; if that fails too, the missing seqs remain detectable (amendment E).
- **Verification:** `verify_session` recomputes continuity; replay-complete ⇔ all seqs present, no gap records, no truncation/corruption, final part closed cleanly. `tools/inspect_recording.py` prints the report (exit 0/1/2).
- **Replay (C3 subset):** `iter_raw_events(session)` → `Normalizer` → `MarketEngine` reproduces the live engine state exactly (tested end-to-end against a fake TWS). A full replay tool arrives in C6.

---

## 8. Tape, classification, bars, metrics (C4–C8, unchanged plan)

Tape (bounded, BUY/SELL/UNKNOWN with method + confidence; delta split buy/sell/unknown),
fill-vs-cancel attribution window, bars 30 s/1 m/5 m (UTC aligned, closed by clock ticks —
precision revisited in C5), order-flow metrics (imbalance, OFI, microprice, velocity, level
episodes, absorption, replenishment, icebergs, sweeps, spread, impact, exhaustion inputs).
L1 (reqMktData) is never an input to these (D2).

---

## 9. Health, errors, recovery

| Code(s) | Class | Handling |
|---|---|---|
| 317 | DEPTH_RESET | active generation ⇒ book reset (new epoch), rebuild must re-validate |
| 316 *(verify live)* | DEPTH_HALTED | active depth ⇒ invalidate ⇒ paced resync |
| 1100 / 1300 | CONNECTIVITY_LOST | connection LOST, books invalidated |
| 1101 | RESTORED_DATA_LOST | books invalidated, resubscribe everything |
| 1102 | RESTORED_DATA_KEPT | connection OK; stale book resynced |
| 2103 / 2110 | FARM_BROKEN / SERVER_CONNECTIVITY_BROKEN | degraded, books invalidated; cleared by 2104/1101/1102 |
| 2104 | FARM_OK | clears farm-broken |
| 2105/2106/2107/2108/2119/2157/2158, 21xx | INFO | never changes stream state |
| 309 / 101 / 10190 *(verify)* | CAPACITY_EXCEEDED | active stream UNAVAILABLE (fail safe) |
| 354 / 10090 / 10168 / 322 / 321 / 10189 *(verify)* | SUBSCRIPTION_REJECTED | active stream UNAVAILABLE |
| 10167, marketDataType ≠ 1, delayed tick types | DATA_NOT_LIVE | hard block, books invalidated; cleared by a later LIVE on the active L1 generation (book must still resync) |
| 10197 | SESSION_CONFLICT | hard block + bounded recovery (below) |
| unknown code on an ACTIVE subscription | UNKNOWN | stream UNAVAILABLE (fail safe) |
| "read-only" in message | READONLY_REJECTED | critical alert |

**10197 recovery** (engine state machine `NONE → BLOCKED → RECOVERING → NONE | EXHAUSTED`):
an attempt (`RawControl conflict_recovery_attempt`, paced every `conflict_retry_interval_s`,
only if the full resubscription fits the request budget) cancels and re-requests all streams.
The conflict clears **only** when all of the following are proven after the attempt started:
no new 10197; every required subscription re-created with a new generation and no active
error (AllLast needs no new print); fresh BidAsk data; fresh depth and book **VALID**;
`marketDataType == 1` on the new L1 generation; connection CONNECTED and farm OK.
Time can only FAIL an attempt (`recovery_attempt_timeout_s`), never prove it. After
`conflict_max_attempts` failures: EXHAUSTED, required streams UNAVAILABLE, critical alert,
no automatic retries until reconnect (new `nextValidId` after a closed/lost connection) or
operator retry (`kill -USR1 <pid>`).

**Depth resync** is paced (`resync_min_interval_s`) and budgeted (`resync_max_per_window` per
`resync_window_s`); an exhausted budget raises a critical alert and stops automatic depth
resubscription until reconnect/operator retry. Missing subscriptions (e.g. a request that was
rate-limited) are re-issued with the same pacing.

`market_data_ok` (per instrument) = connected ∧ farm OK ∧ no conflict ∧ live data confirmed on
the active L1 generation ∧ contract defined ∧ required streams subscribed without error
(depth/BBO/L1 ACTIVE; trades may be quiet) ∧ book VALID ∧ no critical alert. Silence alone never
appears in the reasons. Session hours remain context only (decision 9).

---

## 10. Safety: read-only enforcement

1. TWS **Read-Only API** stays enabled.
2. **`ReadOnlyClient`** — allowlist/default-deny of every EClient method; forbidden set never allowlisted; message-id guard on `sendMsg`/`sendMsgProtoBuf`; wire guard on every connection object; every blocked attempt **latched** in `readonly_violations` (ibapi swallows some exceptions) and surfaced as a critical alert by the heartbeat.
3. **Static tests** over `hermes/` **and** `tools/`: no forbidden method names, no `ibapi.order*` imports, no EClient import/subclass/construction outside `readonly.py`, `ReadOnlyClient` constructed only by the supervisor (or standalone diagnostics in `tools/`), TWS request methods called only from `RequestGateway` inside the package.
4. Config refuses `orders_enabled = true`, `read_only = false`, `client_id = 0`.
5. No `hermes/execution` package.
6. ibapi pinned to **10.45.1**.
7. End-to-end tests assert that the fake TWS never receives any order-related message id.

---

## 11. Future execution provisions (not implemented)

Separate execution connection/clientId; broker-native protective stop with entry; broker as
source of truth (startup reconciliation); entry gating on `market_data_ok` + `UserTradingWindow`;
single-use TTL approvals; idempotent `orderRef`; partial fills/rejects; stops never widened after
risk reduction; standalone kill switch; position management continues on stale data/outside hours.

---

## 12. Deployment

Mac (Python 3.11 venv) now; Mac Mini Intel later. Runtime deps: ibapi 10.45.1 (TWS
distribution), msgpack 1.2.2 (wheels for macOS x86_64/arm64). No IBC in Phase C. Prevent
sleep/App Nap during sessions.

---

## 13. Package layout (C3)

```
hermes/
  config.py                    TOML → validated frozen dataclasses
  core/clock.py latency.py telemetry.py logging_setup.py
  ibkr/raw_events.py codes.py errors.py market_rules.py contracts.py normalizer.py
       readonly.py gateway.py adapter.py session.py
  market/events.py pricegrid.py orderbook.py health.py engine.py snapshot.py
  storage/codec.py recorder.py reader.py
  app/run_live.py              python -m hermes.app.run_live [--duration N]
tools/inspect_recording.py     recording verification report
tools/phase_b/*.py             Phase B diagnostics (ReadOnlyClient)
config/hermes.toml
tests/unit tests/property tests/safety tests/integration (fake TWS) tests/live (manual)
```

---

## 14. Test plan status

- **C1/C2:** config, clock, PriceGrid, events, ReadOnlyClient (3 layers + latch), static scan, order book unit + property tests.
- **C3:** error table, contract resolution, normalizer (mapping, anomalies, generations), engine (generations, silence ≠ failure, 317, 1100/1101, farm, rejections, delayed data, 10197 proven/timeout/exhausted/budget reset, owner guard, determinism), codec round-trip for every raw type, recorder (never blocks, overflow gaps, write-error gaps, undeclared gaps, truncation, rotation), inspect tool, gateway (ReadOnlyClient only, send timestamps before send, unique reqIds, local failure, rate limits), pipeline (sequencing, timer ticks, never raises, owner guard, concurrency), adapter signature conformance with EWrapper, latency histograms, **end-to-end against a fake TWS speaking the real ibapi 10.45 protobuf wire protocol** (healthy run + live/replay equivalence, 317 + resync + late old-generation callbacks, 10197 recovery, 10197 budget exhaustion, reconnect, connection refused, no order message ever sent).
- **Live (manual):** `tests/live/test_live_smoke.py` / `python -m hermes.app.run_live --duration 60`.
- **C4–C9:** as planned (tape/classifier, bars, metrics scenarios, full replay tool, soak).

---

## 15. Milestones

C1 ✅ · C2 ✅ · C3 ✅ (live validation pending) · C4 BBO/tape/classifier · C5 bars/session (tick
precision) · C6 replay tool + equivalence harness · C7 metrics L1 · C8 metrics L2 · C9 health
hardening/soak.

---

## 16. IBKR behavior requiring live validation

1. Exact codes for tick-by-tick capacity/rejection (10189/10190) and depth halt (316).
2. How TWS reports and clears 10197 (whether `marketDataType` or other messages accompany it).
3. Whether CME depth arrives via `updateMktDepth` or `updateMktDepthL2` for this account, and whether a 317 is followed by a full re-send of the book.
4. BidAsk vs depth top-of-book agreement/latency under real load (calibrates `bbo_mismatch_grace_ms`, tolerance).
5. Behavior of subscriptions across 1101/1102 and farm 2103/2104 transitions.
6. Real callback rates, `msg_queue` backlog, callback/core latency percentiles on the MacBook.
7. Contract details for MNQ Dec 2026: `marketRuleIds`/`validExchanges` alignment, `minTick`, multiplier format.
