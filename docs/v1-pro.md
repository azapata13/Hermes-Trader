# Hermès V1 Pro

V1 Pro adds setup **memory** on top of V1:

- what happened to each setup after its entry window closed;
- an objective outcome measurement for every setup;
- explainable historical comparison;
- an LLM that interprets those facts.

Nothing in V1 Pro can trade. `read_only = true`, `orders_enabled = false` and
`mode = "HUMAN_APPROVAL"` are unchanged, and ENTER is still human intent only.

**State: V1 PRO CODE-COMPLETE — LIVE VALIDATION PENDING.**

## Architecture

```text
MarketEngine ──► DecisionRuntime ──► C9 candidate → LifecycleTracker → SafetyPolicy → ApprovalPayload
                      │                (entry window: the ONLY approval authority, unchanged)
                      └──► SetupTracker (D3/D4/D6)   reads lifecycle + engine, never writes back
                               │  setup events (rare)
                               ├──► setups.json (per session)      → replay comparison
                               ├──► setups.jsonl (cumulative store) → D5/D7 analytics, History
                               └──► Slack setup messages (no buttons)  +  D8 Sol interpretation (reply)
```

| Module | Role |
|---|---|
| `hermes/decision/tracking.py` | Setup phases, shadow outcome (MFE/MAE), re-arm links (deterministic, replayed) |
| `hermes/decision/features.py` | Versioned categorical characteristics for similarity |
| `hermes/replay/setups.py` | `setups.json` save / load / live-vs-replay comparison |
| `hermes/analytics/store.py` | Append-only `setups.jsonl` of finished setups |
| `hermes/analytics/stats.py` | Outcome statistics, similarity v1, dispositions, missed-opportunity report |
| `hermes/analytics/llm_context.py` | Versioned LLM context, validation, prompt, output guard |
| `hermes/slack/pro.py` | History and re-arm note providers, Sol interpretation worker |
| `tools/setup_report.py` | Missed-opportunity / shadow-setup report |

## D3 — setup lifecycle (STALE ≠ DEAD)

The C9 lifecycle (`ACTIONABLE → STALE / EXPIRED / INVALIDATED / BLOCKED`) still decides whether an
entry may be approved. `SetupTracker` follows the setup behind each ACTIONABLE candidate:

| Phase | Meaning |
|---|---|
| `ACTIVE` | C9 candidate ACTIONABLE (entry window open) |
| `MONITORING` | Window closed by STALE / EXPIRED / BLOCKED; still followed |
| `REARMED` | While MONITORING, a **new** ACTIONABLE candidate of the same direction appeared (see D6) |
| `INVALIDATED` | Original stop touched by an eligible print, or C9 structural invalidation |
| `ENDED` | Outcome horizon reached, or the measurement became incomplete |

Each setup keeps:

- identity: setup_id, proposal_id, direction;
- original entry / stop / risk, and the creation and expiry times;
- window close status, reasons and time; monitoring start;
- invalidation reason and time;
- re-arm link (`rearmed_by` / `rearm_of`), re-arm time and the new candidate's fresh entry;
- characteristics.

Every transition is one audited event `(seq, event time, setup_id, kind, from, to, detail)`.
Tracking uses event time only, applies duplicate / out-of-order raw events once, and costs O(1)
per event when nothing is tracked. It never changes a C9 status, the C9 journal or the C9
decision fingerprint (tested).

## D4 — outcome (shadow MFE / MAE)

The outcome is a **shadow** measurement:

- it assumes a hypothetical fill at the proposed entry reference at creation time;
- it measures eligible prints only (never the book, never a synthetic price).

What it records:

- MFE and MAE (units → points → $ at `point_value_usd`) and the first time each was reached;
- first-touch times of +1R, +2R and +3R;
- stop touch (stop-first);
- signed result at 1 / 3 / 5 / 10 / 15 min: the first eligible print at or after each horizon;
- max favorable excursion vs the original entry after the window closed.

Rules:

- Excursions freeze at the first stop touch.
- Outcome status: `OPEN` / `STOPPED` / `COMPLETE` (15 min horizon) / `INCOMPLETE`.
  - INCOMPLETE means a tape continuity break (reconnect, trades resubscription), a missing
    instrument or the session end.
  - INCOMPLETE outcomes are kept but excluded from statistics; nothing is interpolated.
- It is never called profit or P&L.

## D5 — shadow / missed setups

Every finished setup is written to `<data>/setups/setups.jsonl` (default
`~/hermes-data/setups/setups.jsonl`, override with `HERMES_SETUP_STORE`) with `"execution": "SHADOW"`.

- V1 never writes `"REAL"` rows: there is no execution layer.
- Statistics never mix SHADOW and REAL.
- Dispositions are joined from the approval journal:
  - `APPROVED_INTENT` (human intent, still shadow);
  - `REJECTED`;
  - `INVALIDATED`;
  - `EXPIRED_UNAPPROVED` (missed);
  - `OPEN_WINDOW`.

`tools/setup_report.py` reports:

- counts by disposition;
- +1R / +2R / +3R and stop-first rates;
- median MFE / MAE and median results at 5 / 10 min;
- setups that continued after the window closed;
- STALE / EXPIRED setups that then moved +20 pt or +2R;
- stops hit within 60 s;
- low-MAE / high-MFE setups;
- time-to-MFE distribution;
- best setups by excursion quality = MFE / (MAE + 1 tick), which measures quality, not profit.

The report always prints `REAL TRADES none executed` on its own line.

## D6 — re-arm / fresh entry

A re-arm is never "the old signal is still valid, enter at the old price". It exists only when the
full candidate engine and SafetyPolicy create a **new ACTIONABLE candidate** of the same direction
at a later 30 s decision point while an older setup is MONITORING. The new candidate brings:

- a new setup_id and proposal;
- a fresh entry, stop and risk;
- a new timestamp and a new validation.

The old setup only records the link and the new entry. These can never re-arm:

- an old record;
- a non-actionable record;
- an invalidated or already re-armed setup;
- a candidate of the opposite direction.

## D7 — historical similarity and History

Each setup carries versioned categorical characteristics:

- direction, 5m / 1m / 30s results, VWAP relation;
- tape, OFI and sweep votes relative to the direction;
- session, 30-min UTC bucket, risk bucket.

**Similarity v1:** two setups are comparable when `direction, regime_5m, setup_1m, trigger_30s,
vwap, tape, session` are all equal. There is no weighting and no model, so it is explainable field
by field.

The Slack line `History: NN% +1R · N similar setups (shadow)` appears only with **≥ 30**
comparable recorded shadow setups. Its definition, comparable-set definition and source are
carried with it, and it is never shown with 29 or fewer.

## D8 — LLM (Sol) context

```text
engine facts → build_context() (hermes-llm-context/1) → validate_context() → render_prompt()
     → Sol → guard_output() → thread reply "🧭 Sol — interpretation only (no authority, no order)"
```

- **Context.** Every number comes from the engine. History is included only when ≥ 30; otherwise
  `available: false`. Missing, null, contradictory or under-sampled contexts are rejected and Sol
  is not called.
- **Guard.** At most 4 lines are kept. A line is dropped if it contains a number that is not in the
  context (invented price, stop, statistic) or instruction-like wording (orders, `orders_enabled`,
  `read_only`, ENTER, stop moves, "ignore the rules").
- **When Sol runs.** Only on WINDOW_CLOSED / REARMED, on its own bounded thread, and only with
  `HERMES_AI_ENABLED=1` and `OPENAI_API_KEY`. Never per tick.
- **No path to the engine (tested).**
  - LLM and analytics modules import nothing from the broker, decision runtime, lifecycle,
    safety, response or config.
  - The worker's only effect is `transport.reply_text`.
  - A malicious interpreter output changes no approval, journal, status, fingerprint or setting.

## D9 — Slack

- The first message is the D1.9 proposal, unchanged: decision-oriented, ENTER / REJECT,
  `Intent only — no order sent · valid N s`.
- After its window closes, the **same** message is updated with a setup view that has **no
  buttons**:
  - `👁 SETUP MONITORING`: original entry / SL, window closed, now, MFE, MAE, *"Old entry NOT
    available. A fresh 30 s trigger is required for a new entry window."*;
  - `↻ SETUP RE-ARMED`: points to the new, separately posted proposal;
  - `✖ SETUP INVALIDATED`;
  - `■ SETUP ENDED`: shadow +1R / +2R / stop-first.
- An accepted human decision stays visible on these updates.
- A re-armed proposal shows one line naming the earlier setup whose entry expired.

## Warm start, replay, persistence

| File | Written | Content |
|---|---|---|
| `<session>/decisions.json` | shutdown | C9 decision journal (unchanged format) |
| `<session>/setups.json` | shutdown | setup events, final views, tracking fingerprint (`hermes-setups` v1) |
| `<data>/setups/setups.jsonl` | on each finished setup (async) | cumulative finished setups (schema 1) |

- **Replay.** `replay_session` rebuilds `setups.json` with the same runtime and reports
  EQUIVALENT / MISMATCH.
  - A recording without `setups.json` (made before V1 Pro) gives "not applicable".
  - A warm-started session re-applies its warm-start source first (D2.4), then compares.
- **Restart and warm start.** A new process starts with an empty tracker; replayed history never
  creates a tracked setup. Setups still open at shutdown are recorded INCOMPLETE (`session_ended`).
- **Old data.** The store loader skips and counts malformed lines, unknown schemas, duplicates and
  unfinished rows. Rows without newer optional fields stay usable.

## Known limitations

- **NOT LIVE-VERIFIED.** The tracker, Slack setup messages and Sol interpretation have run only on
  synthetic / replayed data.
- The outcome assumes a fill at the proposed entry and ignores slippage, fees and queue position.
  It measures setup quality, not trading results.
- The 30-min time bucket is UTC; there is no volume or volatility characteristic in similarity v1.
- History stays hidden until ≥ 30 comparable finished setups are recorded. With a new store, that
  takes many sessions.
- The Sol context's `age_s` uses wall-clock time at interpretation (display only; never journaled).
