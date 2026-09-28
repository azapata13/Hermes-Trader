# Hermès — Phase C9 final report

Status: **ACCEPTED** (2026-09-28). Tag: `phase-c9`. Runtime version `0.9.0-c9` (pyproject `0.9.0+c9`).

C9 adds a deterministic **HUMAN_APPROVAL decision layer** on top of the C1–C8 market engine. It is a
proposal system only: **no order is placed, `placeOrder()` is never called, the TWS API stays behind
`ReadOnlyClient`, there is no autonomous mode, no Slack integration and no broker adapter.**

## 1. What C9 delivers

| Milestone | Content |
|---|---|
| C8 fix | Follow-through is judged on the midpoint prevailing at its deadline (no look-ahead). |
| C9a | `DecisionContext`: pure function of an immutable `MarketSnapshot` (data quality, price, 5 m / 1 m / 30 s bar context, session / RTH VWAP, C7/C8 evidence, MBP epistemic notes). |
| C9b | `SetupCandidate`: one conservative multi-timeframe continuation family; NONE is first-class; entry = best ask/bid; stop = max(10 pt, structure + 2 ticks), > 12 pt ⇒ NONE; no take-profit. |
| C9c | Candidate lifecycle: deterministic `setup_id`, NONE / BLOCKED / ACTIONABLE / EXPIRED / STALE / INVALIDATED, temporary holds for a transiently non-priceable VALID book. |
| C9d | One `SafetyPolicy` for creation, lifecycle and approval; stable bare reason codes; no override input; C7/C8 availability re-checked continuously; RTH_ONLY with opening/closing buffers. |
| C9e | Structured `Reason(source, code, detail, severity)`; `ApprovalPayload` with `proposal_id` (immutable proposal) and `approval_view_id` (exact view shown); deterministic text view; pure `HumanApprovalResponse` (ENTER/REJECT = intent only). |
| C9f | One `DecisionRuntime` wired into the live runtime and into replay; compact decision journal and decision checkpoints (`decisions.json`), separate from market checkpoints; live-vs-replay decision equivalence; behavior-identical snapshot and hashing optimizations; acceptance and benchmark tools. |

Detailed design: `docs/ARCHITECTURE_PHASE_C.md` §8d–8e.

## 2. Final acceptance — fresh real MNQ live run

Run by Felipe with `python tools/c9_acceptance.py --live 60` (TWS live port, LIVE CME data, READ-ONLY).

Session: **`~/hermes-data/recordings/2026-09-28/20260928T175749Z-86453`**
(report written next to it: `c9_acceptance.json`).

| Check | Result |
|---|---|
| Live run healthy (connection, contract, LIVE data, read-only, replay-complete) | **PASS** |
| MARKET live vs replay EQUIVALENT — 26/26 checkpoints, final hash match | **PASS** |
| DECISION live vs replay EQUIVALENT — 2/2 journal records, 2/2 decision checkpoints; setup_ids, proposal_ids, approval_view_ids, reason codes and decision fingerprints match; final fingerprint match | **PASS** |
| Second FAST replay IDENTICAL (market) | **PASS** |
| Second FAST replay IDENTICAL (decision) | **PASS** |

- Market final hash (second replay): `b931058a62b2907b735972cc495a3b70ef18a3a0e7c541f2ea97821c69d308fc`
- Decision final fingerprint: `19fd691d6755b2f63b68bd728704dec8d9ef2461fa4cd3a1f6d9ff2354d63a6b`
- Decision counts: 2 evaluations — 0 LONG, 0 SHORT, 2 NONE; 0 lifecycle transitions.
  NONE is a valid outcome: the run validates deterministic behavior, not the occurrence of a setup.

Test suite at C9f: 726 passed, 2 deselected (live-only); ruff clean; `git diff --check` clean.
Market `HASH_VERSION` unchanged (4); the decision state never enters the market state hash.

## 3. Safety invariants (tested)

- `hermes/decision` imports only config / market / fingerprint / decision code; no broker, app, network,
  clock or RNG modules; no order vocabulary (static isolation tests, including the C9f runtime and
  `hermes/replay/decisions.py`).
- `[decision].mode` accepts only `HUMAN_APPROVAL`; `[safety].orders_enabled` must be false.
- SafetyPolicy accepts no override; no LLM / memory / shadow / learned input can remove a hard block or hold.
- `approval_allowed_now` requires a fresh passing approval-time SafetyResult for that `setup_id`.
- `HumanApprovalResponse.authorizes_execution` is hard-wired False; `execution_prerequisites` can never
  be satisfied in Phase C (`risk_eligibility_policy_not_implemented`, `execution_layer_disabled`).
- UNKNOWN trades are never redistributed; MBP is never interpreted as MBO; walls alone are not signals.

## 4. Performance

Synthetic figures come from `tools/c9_benchmark.py --synthetic 5` (busy MNQ-like market, ~210 raw
events/s, generated); the real-recording figure comes from the acceptance session above.

| Component (synthetic, median / p99) | Before (C9e code) | After (C9f) |
|---|---|---|
| `engine.snapshot()` | 3 078 / 6 118 µs | 375 / 934 µs |
| C7 metrics snapshot | 1 072 / 2 130 µs | 60 / 175 µs |
| C8 structure snapshot | 1 680 / 3 409 µs | 55 / 167 µs |
| C8 patterns snapshot | 66 / 160 µs | 59 / 153 µs (unchanged) |
| Market state hash | 31.7 / 82.6 ms | 19.8 / 67.2 ms |
| Replay throughput (market only) | ~16.9k raw/s | ~15.9–16.1k raw/s |

Decision layer (synthetic): DecisionContext ~116 µs and candidate evaluation ~138 µs once per 30 s
bar; lifecycle check ~53 µs per event while a candidate is ACTIONABLE; SafetyPolicy facts + evaluate
~44 µs; decision runtime 1.6 µs per event otherwise; decision fingerprint ~33 µs per journal event;
approval payload ~1.1 ms once per ACTIONABLE candidate (not on the hot path); peak RSS ~81 MB.

The snapshot optimization (exact cumulative rolling sums, `hermes/market/rolling.py`) and the faster
`canon()` are behavior-identical: per-event equality against the original rescans, byte/hash equality
of the canonical state, and before/after replays against the C9e code (identical market checkpoints,
final hashes and decision fingerprints).

**Real recording (acceptance session):** market state hash ~11 ms median, ~41 ms p99.

## 5. Remaining technical debt (must be resolved before any execution phase)

1. **Market checkpoint hashing on the market-data thread.** `market_state_hash` serializes the whole
   bounded engine state on the dispatch thread at bar closes, health changes and every 10 000 raw events:
   ~11 ms median / ~41 ms p99 on the real acceptance recording (~20 / ~67 ms on the busy synthetic
   market). Harmless for a read-only proposal system, but an execution layer must never wait behind it.
   Before real orders or stops exist, move checkpoint hashing off the order-critical path (incremental
   hashing, or a copy-on-write state capture hashed on a separate thread) and prove the checkpoint
   sequence unchanged by replay.
2. Deprecated legacy candidate reason strings remain for internal stats only (never parsed for
   decisions); remove after nothing depends on them.
3. `apply_c7b2.py` at the repository root is a stale one-off C7 patch script (unreferenced); candidate
   for removal in a housekeeping change.

## 6. Out of scope (not started)

C10, Slack ENTER/REJECT transport, IBKR execution / broker adapter, RiskEligibilityPolicy
(e.g. max ~5 trades/day), autonomous mode.
