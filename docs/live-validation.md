# Hermès — live validation runbook (D2.3 → D1.9)

Scope: validate, on the real market, the chain

```text
TWS → IBKR API → MNQZ6 → BBO / tick-by-tick → CME Level II (DOM) → tape / aggressor
    → 30s / 1m / 5m bars → VWAP → OFI / sweep → decision engine → ACTIONABLE candidate
    → Slack D1.9 message (+ TWS screenshot) → HUMAN decision
```

and **stop there**. Hermès is READ-ONLY: there is no order path in the code
(`tests/safety/test_no_order_paths.py`), a Slack ENTER is recorded as human intent
only, and `HumanApprovalResponse.authorizes_execution` is hard-wired false.

> Never click ENTER during this validation. REJECT is allowed (it only closes the
> human workflow). Nothing in this runbook changes `read_only`, `orders_enabled`
> or the decision mode.

---

## 1. Safety invariants (check before every run)

| Setting | Where | Required value |
|---|---|---|
| `orders_enabled` | `config/hermes.toml` `[safety]` | `false` |
| `read_only` | `config/hermes.toml` `[ibkr]` | `true` (ReadOnlyClient refuses any order message) |
| `mode` | `config/hermes.toml` `[decision]` | `"HUMAN_APPROVAL"` (the only accepted value) |
| TWS API | TWS → Global Configuration → API → Settings | **Read-Only API checked** |

```bash
grep -nE '^(orders_enabled|read_only|mode)\b' config/hermes.toml
```

Expected: `orders_enabled = false`, `read_only = true`, `mode = "HUMAN_APPROVAL"`.
The run summary must end with `read_only_violations 0`.

## 2. TWS prerequisites

| Item | Value in the repo config | Notes |
|---|---|---|
| Host / port | `127.0.0.1:7496` (`[ibkr]`) | 7496 = live TWS. Never expose the API port outside localhost. |
| clientId | `110` | Error 326 means the clientId is already in use: stop the other Hermès / API client. |
| API sockets | "Enable ActiveX and Socket Clients" | Trusted IP 127.0.0.1 only. |
| Market data | CME Real-Time **including Level II** (depth) | Without L2 the depth request is rejected (e.g. 354 / 10092) and `depth:` reasons appear. |
| Depth lines | `depth_rows = 10`, `depth_smart = false` | Error **309** = depth lines exhausted: close BookTrader / Level II / DOM windows that use the same lines, then restart Hermès. |
| One session only | — | Error **10197** = another live session (TWS on another machine, mobile, web) holds market data. Hermès retries 3 times, then raises `market_data_conflict_retries_exhausted`. |
| Contract | `[instrument]` MNQ FUT CME USD, `MNQ`, `202612` | Resolves to **MNQZ6**. The TWS window month does not affect the API, but a TWS DOM window can consume depth lines (309). |

Error classification lives in `hermes/ibkr/errors.py`. IBKR errors are counted in
`~/hermes-data/logs/hermes-telemetry.jsonl` (`engine.errors_by_code`) and in the
recording (`tools/depth_report.py`).

## 3. Environment

Never print, log or commit the values. `scripts/start_hermes.sh` is intentionally
**untracked** on the Mac mini (it may load a local env file); keep it that way.

| Variable | Required | Default (from the code) | Purpose |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | for Slack | — | `xoxb-…`; all three Slack variables or none (partial = Slack disabled with an error) |
| `SLACK_APP_TOKEN` | for Slack | — | `xapp-…` (Socket Mode) |
| `HERMES_SLACK_CHANNEL_ID` | for Slack | — | channel **ID** (`C…`), not a name |
| `HERMES_SLACK_APPROVER_IDS` | recommended | unset | comma-separated Slack user IDs allowed to act (D2.8). Others are ignored and audited; unset = ENTER recorded as intent only, flagged `approver_allowlist_missing`; malformed = Slack disabled |
| `OPENAI_API_KEY` | for Luna/Sol only | — | optional; without it the screenshot is posted without AI notes |
| `HERMES_AI_ENABLED` | no | `0` | Luna/Sol notes on the screenshot |
| `HERMES_SCREENSHOT_ENABLED` | no | `0` | TWS screenshot in the Slack thread (macOS `screencapture`) |
| `HERMES_TWS_WINDOW_ID` | no | auto-detect | force the TWS window id if auto-detection picks the wrong window |
| `HERMES_SCREENSHOT_DIR` | no | `~/hermes-data/screenshots` | |
| `HERMES_LUNA_MODEL` / `HERMES_SOL_MODEL` | no | code defaults | |
| `HERMES_SOL_ON_COMPLEX` | no | `1` | Sol only on conflicting setups |
| `HERMES_SLACK_JOURNAL` | no | `~/hermes-data/logs/human_approvals.jsonl` | append-only approval journal |
| `HERMES_SLACK_CHAT_JOURNAL` | no | `~/hermes-data/logs/slack_chat.jsonl` | conversational journal |
| `HERMES_WARM_START` | no | `1` | `0` disables the warm start (cold start) |
| `HERMES_WARM_START_MAX_AGE_S` | no | `43200` | newest recording older than this is ignored |

Screenshots need macOS **Screen Recording** permission for the terminal / Python
that runs Hermès (System Settings → Privacy & Security → Screen Recording).

## 4. Start

```bash
cd ~/hermes-trading
git status            # scripts/start_hermes.sh may be untracked; nothing else should be
git log -1 --oneline
caffeinate -dimsu &   # keep the Mac awake for the whole run
scripts/start_hermes.sh            # or: python -m hermes.app.run_live
# smoke test:  python -m hermes.app.run_live --duration 120
```

Signals (handled by `Supervisor.install_signal_handlers`):

- `Ctrl-C` / `SIGTERM`: clean stop, recording finalized, summary printed.
- `kill -USR1 <pid>`: operator retry. Resets the 10197 budget **and** the depth-resync
  budget, after you fixed the cause (closed a DOM window, freed a session).

## 5. What the logs must show (`~/hermes-data/logs/hermes.log`)

In this order:

1. `WARM START | applied | … | 30s=… 1m=… 5m=…` or `WARM START | skipped | reason=…`.
   - If the previous recording ended with an alert, you also see
     `WARM START | recording ended with alert(s) … | historical (previous process), not carried into live health`.
     This is information only: the alert is **not** live state.
2. `session phase wait_connect -> resolving_contract`, then `resolving_contract -> market_rule -> streaming`.
   - Since D2.3, a warm-started process always resolves its own contract.
   - A plain reconnect inside the same process goes straight to `streaming`.
3. The console line turns `OK`, with the book `VALID`, all streams `active` and the tape counting.
4. No `depth resync … reason=…` storm. An isolated resync with a reason is normal
   (IBKR 317 resets, data anomalies).
5. No `ALERT …` line.
   - `depth_resync_budget_exhausted`: the depth subscription keeps failing; the log line names the last reason.
   - `market_data_conflict_retries_exhausted`: 10197.

## 6. Health layers: do not mix them

| Layer | Meaning | Where to read it |
|---|---|---|
| **Process OK** | No read-only violation, no internal (processing) error, recording replay-complete. | summary `process_ok` |
| **Market data HEALTHY** | `market_data_reasons()` is empty: connection CONNECTED; no farm break; no 10197; data live; contract `defined`; depth, BBO and L1 subscribed, error-free and active (trades only need to be subscribed); L1 live confirmed (marketDataType 1 on the current generation); book `VALID`; **no alert**. | console `OK` / `NOT-OK: …`; summary `market_data_ok_before_shutdown`, `problems` |
| **Depth HEALTHY** | Depth stream active on the current generation and book `VALID`: ≥ 5 valid rows, sorted, not crossed beyond the 250 ms grace, top of book equal to the tick-by-tick BBO, stable for `settle_ms = 500`. | console `book=VALID`; summary `depth_ok_before_shutdown` |
| **Candidate ACTIONABLE** | Decision layer `ACTIONABLE` **and** a fresh SafetyPolicy pass (`approval_allowed_now`). Requires market data HEALTHY, RTH entry window, valid calendar, no active data gap, C7/C8 available, … (`hermes/decision/safety.py`). | console `decision …`; `decisions.json`; Slack shows ENTER only in this state |
| **Run HEALTHY** | The conjunction printed as `RESULT HEALTHY` / `UNHEALTHY`. | end of run summary |

UNHEALTHY is always explained by `problems` / `NOT-OK: <reasons>`. Typical reasons:

- `connection:*`
- `contract:*`
- `depth:requested` (no depth yet)
- `depth:error_<code>`
- `book:building(insufficient_depth)`
- `book:stale(...)`
- `l1:live_not_confirmed`
- `alert:<key>`

Warm start note: replayed history (bars, tape, VWAP) is reused. Replayed **health** never
is: a warm-started process starts with no alert, a fresh 10197 budget, unsubscribed
streams and an invalidated book (D2.3).

## 7. Slack D1.9 criteria (on a real ACTIONABLE candidate)

Message, in this order (`hermes/slack/render.py`, `summary.py`):

1. `🟢 LONG MNQZ6 · Entry …` / `🔴 SHORT …`, then `SL … · Risk … pt ($…)`.
2. `5m ▲ … · 1m ▲ … · 30s ▲ …`, then `VWAP … · Volume LOW|NORMAL|HIGH · Buyers N% | Sellers N% | Balanced`,
   then `Tape ✓ · OFI ✓ · Sweep ✓`.
3. At most 3 reasons, in reading order: tape → 5m/1m/30s alignment → VWAP → others.
4. `⚠` lines only for real conflicts: opposite flow, trigger volume mostly UNKNOWN,
   price on the wrong side of the full-session VWAP. At most 2.
5. `History: NN% win · N similar setups` **only** with ≥ 30 recorded comparable setups and
   explicit outcome and selection definitions. No such source exists yet, so **no History line is expected**.
6. Buttons `[ ENTER ] [ REJECT ]`. ENTER is present only while approvable.
7. Footer exactly `Intent only — no order sent · valid N s`.

Not visible: internal ids, reason codes, take-profit, data-feed caveats (they are in the
journals / logs).

Thread: `📸 TWS · MNQZ6 LONG @ …` screenshot (if enabled), plus at most 2 Luna lines and
2 Sol lines (Sol only on conflicts).

Approval behavior to observe (**REJECT only, never ENTER**):

- REJECT closes the workflow and is journaled.
- An old, expired, unknown or safety-blocked view fails closed.
- Malformed Slack payloads are ignored (D2.5).

## 8. Stop and verify

```bash
# Ctrl-C in the Hermès terminal, then:
python tools/replay_report.py "$(ls -td ~/hermes-data/recordings/*/* | head -1)" --verify
python tools/depth_report.py  "$(ls -td ~/hermes-data/recordings/*/* | head -1)"
```

`replay_report --verify` must show market and decision equivalence (`EQUIVALENT`).

- Since D2.4, a warm-started session replays its warm-start source first. You see
  `warm start re-applied from …` in the notes.
- If the source recording was deleted, the report says the comparison is **not applicable**.
  That is not a mismatch.

## 9. Live checklist

1. TWS open and logged in; no other live session (phone / web / second TWS).
2. API settings: socket clients on, Read-Only API on, port 7496, localhost only.
3. MNQZ6 depth available (CME L2); no BookTrader / DOM window holding the depth lines.
4. `git status` / `git log -1` as expected; `config/hermes.toml` invariants (§1).
5. Slack variables present; `HERMES_SCREENSHOT_ENABLED=1` if the screenshot is part of the test.
6. `caffeinate -dimsu`.
7. Start Hermès (§4) and read the startup lines (§5).
8. Console `OK`, book `VALID`, streams active (§6).
9. Wait for a real candidate during RTH. Do not fabricate one.
10. Inspect the Slack message (§7) and the screenshot in its thread.
11. Never ENTER. REJECT only if you want to close it.
12. `Ctrl-C`; keep the summary output; run §8.
13. Report: summary lines `process_ok`, `market_data_ok_before_shutdown`, `depth_ok_before_shutdown`,
    `warm_start`, `read_only_violations`, `slack`, plus the replay verification.

## 10. Verified in CI vs. NOT LIVE-VERIFIED

Covered by tests (fake TWS speaking the ibapi wire protocol + replay):

- cold start;
- warm start;
- reconnect after warm start;
- 10197 budget;
- 317 / structural resync;
- live/replay equivalence, also for warm-started sessions;
- SafetyPolicy blocks;
- Slack rendering, approval fail-closed and malformed interactions.

**NOT LIVE-VERIFIED** until observed on the Mac mini with real TWS:

- MNQZ6 L2 depth reaching `VALID`;
- a real ACTIONABLE candidate;
- the real Slack D1.9 message;
- the TWS screenshot in the thread.
