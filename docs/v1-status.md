# Hermès V1 — status

**State: V1 CODE-COMPLETE — LIVE VALIDATION PENDING.**

V1 is a deterministic, observable, fail-closed, read-only MNQ order-flow assistant. It
proposes setups to a human in Slack and **stops there**. It is not an execution system.

| Area | Status | Evidence |
|---|---|---|
| D1.9 concise Slack decision message | done | `tests/unit/test_slack_summary.py` (goldens) |
| D2.3 warm-started process owns its contract; replayed health is historical | done | `tests/unit/test_warm_start_health.py`, fake-TWS integration |
| D2.4 warm-started sessions reproducible by replay; warm replay failure fails closed | done | integration + unit |
| D2.5 malformed Slack interactions fail closed | done | `tests/unit/test_slack_interaction_parsing.py` |
| D2.6 layered health, resync reasons, warm start in the summary | done | integration |
| D2.7 live contract identity must match the warm-start history | done | `test_warm_start_health.py` |
| D2.8 Slack approver allowlist | done | `tests/unit/test_slack_approvers.py` |
| Live TWS / MNQZ6 / L2 DOM / real candidate / Slack / screenshot | **NOT LIVE-VERIFIED** | `docs/live-validation.md` §9 |

## Architecture

```text
TWS (127.0.0.1:7496, Read-Only API)
  │  ibapi wire protocol, ReadOnlyClient (refuses every order message)
hermes/ibkr     adapter → raw events → RawPipeline ── Recorder (.hrec, replay-complete)
                session.py: connect, contract + market rule, subscriptions, depth resync budget, 10197 recovery
                normalizer.py: price grid, generations, raw → market events
hermes/market   MarketEngine (single writer): order book (DOM), BBO, tape + aggressor classifier,
                30s/1m/5m bars, sessions + VWAP, C7 metrics (OFI…), C8 structure / patterns, health
hermes/decision DecisionContext → SetupCandidate → LifecycleTracker → SafetyPolicy → ApprovalPayload
                HumanApprovalResponse (intent only; execution prerequisites always fail in V1)
hermes/slack    SlackApprovalBridge (outbox thread, Socket Mode), D1.9 renderer, approver allowlist,
                approval journal, TWS screenshot + optional Luna/Sol notes (never on the safety path)
hermes/replay   deterministic replay, market checkpoints, decision journal comparison, warm start
hermes/app      run_live.py: LiveRuntime wiring, telemetry, run summary
```

There is no order path anywhere: `tests/safety/test_no_order_paths.py`,
`test_readonly_client.py` and `test_slack_isolation.py` enforce it. The run summary must show
`read_only_violations 0`.

## Startup and warm start

1. `LiveRuntime.run()` → `_warm_start()` → recorder start → Slack bridge → supervisor.
2. The warm start replays the newest recording (≤ 12 h, same contract spec, contiguous) up to
   its last connection close. Monotonic time is rebased.
3. Then `end_replayed_session()` runs:
   - alerts and the 10197 phase become **historical**: reported, not live;
   - streams are unsubscribed;
   - the book is invalidated (DISCONNECT);
   - the contract becomes `historical`.
4. The live process resolves its own contract and grid (D2.3). If the conId differs from the
   history, the contract fails closed (D2.7).
5. The new recording stores the warm-start provenance. `replay_session` re-applies the same
   history and verifies live equivalence (D2.4). If the source is missing, the result is
   "not applicable", never a fake MISMATCH or a fake EQUIVALENT.

`HERMES_WARM_START=0` gives a cold start.

## Market data and depth

- Depth: `reqMktDepth`, 10 rows, `isSmartDepth=false`. Book `VALID` needs:
  - ≥ 5 valid rows, sorted;
  - not crossed beyond 250 ms;
  - top of book equal to the tick-by-tick BBO;
  - all of it stable for 500 ms.
- Any structural violation or anomaly invalidates the book (STALE, cleared). The session then
  resubscribes, at most every 5 s and 5 times per 300 s. Each resync logs its reason. When the
  budget is exhausted: `depth_resync_budget_exhausted` (live only; never inherited from a replay).
- Silence is not failure: a quiet market keeps a valid book. Staleness comes from evidence
  (disconnect, 1101, 10197, not-live data, generation change), not from timers.

## Health layers

Run summary fields: `process_ok`, `market_data_ok_before_shutdown`, `depth_ok_before_shutdown`,
`warm_start`, and `healthy` (the conjunction, with `problems`). Definitions:
`docs/live-validation.md` §6.

## Safety

Every path to ACTIONABLE goes through `SafetyPolicy`, at creation, at every lifecycle event and
at approval time. It hard-blocks on:

- connection, contract not defined (incl. historical / identity mismatch);
- data not live, 10197, any critical alert, farm broken;
- stream inactive or generation change, book not valid, active data gap;
- classification context, price grid, session calendar;
- outside RTH entry hours or inside the opening/closing buffers;
- `market_data_not_ok`, C7/C8 unavailable.

Candidates expire on event time (trigger bar end + 30 s), go stale on drift (8 ticks) and are
invalidated at structure. Proposals are bound to `proposal_id` / `approval_view_id`; any old,
expired, unknown, duplicated or safety-blocked view fails closed.

## Slack and approval

- Message: D1.9. History is shown only with ≥ 30 recorded comparable setups and explicit
  definitions; no source exists yet, so it is never shown. The footer reads
  `Intent only — no order sent · valid N s`.
- ENTER = recorded human intent; `authorizes_execution` is always false.
- Approvers (`HERMES_SLACK_APPROVER_IDS`):
  - a listed user acts normally;
  - an unlisted or malformed user changes nothing and is audited;
  - with no list, intent may be recorded but is flagged `approver_allowlist_missing`
    (never executable);
  - a malformed list disables Slack.
- Audit trail: `~/hermes-data/logs/human_approvals.jsonl`. Each line carries the user, action,
  clicked / displayed / current view ids, status, result codes and seq / wall times.

## Logs and recordings

| File | Content |
|---|---|
| `~/hermes-data/logs/hermes.log` | lifecycle, warm start, session phases, resync reasons, alerts, Slack approver count |
| `~/hermes-data/logs/hermes-telemetry.jsonl` | periodic reports, `errors_by_code`, book invalidations / resets, latency |
| `~/hermes-data/logs/human_approvals.jsonl`, `slack_chat.jsonl` | Slack audit / conversation journals |
| `~/hermes-data/recordings/<date>/<session>/` | `part-*.hrec` raw events (incl. warm-start provenance in the header), `checkpoints.json`, `decisions.json` |

Verification tools:

- `tools/replay_report.py <session> --verify`
- `tools/depth_report.py <session>`
- `tools/inspect_recording.py`

Secrets (Slack tokens, OpenAI key, approver IDs) never appear in logs, summaries or audit
records; a test enforces it.

## Tests

`python -m pytest -q`. Coverage includes:

- unit, property, safety and replay tests;
- an integration suite in which the real `LiveRuntime` talks to a fake TWS over the ibapi wire
  protocol: cold start, warm start, warm start + reconnect, warm start + replay, missing source,
  10197 budget, 317 / structural resync, contract ownership;
- safety blocks; Slack rendering, approval, malformed payloads and the allowlist.

Tests marked `live` need a running TWS and are deselected by default.

## Known limits (V1)

- **Not live-verified.** MNQZ6 L2 → `VALID`, a real ACTIONABLE candidate, the real Slack
  message and the TWS screenshot still have to be observed (`docs/live-validation.md`).
- No execution layer, no risk-eligibility policy and no position management. By design, ENTER
  authorizes nothing.
- No historical-statistics source; the History line stays hidden.
- Deleting a recording makes the replay verification of later warm-started sessions that depend
  on it "not applicable".
- Ruff: the default rules pass. Some extended / preview-rule findings remain in Phase C code
  (min/max rewrites in market hot paths, naive exchange-local datetimes in `sessions.py`,
  recorder try/except). They were left on purpose; they are not V1 defects.
- The local `scripts/start_hermes.sh` (Mac mini, git-ignored) was not audited from this
  environment; compare it with `scripts/start_hermes.example.sh`.
