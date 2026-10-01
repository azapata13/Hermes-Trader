# Hermès — D1 Slack HUMAN_APPROVAL transport

Status: **code checkpoint**. This milestone connects the C9 `ApprovalPayload` /
`HumanApprovalResponse` model to Slack while preserving the C9 safety boundary.

**D1 does not execute trades.** There is no broker adapter, no `placeOrder`, no
RiskEligibilityPolicy and no autonomous entry. A Slack ENTER is human intent
only and `HumanApprovalResponse.authorizes_execution` remains hard-wired false.

## Architecture

```text
MarketEngine (single writer)
        |
DecisionRuntime / SafetyPolicy
        |
  on decision transition
        v
SlackApprovalBridge.publish_decision()
        |
   bounded in-memory outbox  -------------------------+
        |                                             |
        |                                  Slack outbox worker
        |                                             |
        |                                      Socket Mode / Web API
        |                                             v
        |                                     one Slack message
        |                                     per proposal_id
        |
Slack button ACK (Slack thread)
        |
compact SlackInteraction
        |
inbound queue
        |
SlackApprovalBridge.after_event()  <-- market dispatch thread
        |
fresh current ApprovalPayload + SafetyPolicy re-check
        |
HumanApprovalResponse + append-only approval journal
        |
NO execution
```

Slack network latency never blocks the market-data dispatch thread. The only
work done on the dispatch thread for Slack is queueing immutable views and
processing a bounded number of already-ACKed human interactions. Slack post and
update calls run on a dedicated outbox worker. Human-approval JSONL persistence
runs on a separate journal writer.

## Message lifecycle

One Slack message is used for each immutable `proposal_id`.

- ACTIONABLE + safe now: ENTER and REJECT are shown.
- temporary crossed/unsorted-book hold: the same message is updated and ENTER
  is omitted. Slack does not have a disabled-button state, so omission is the
  fail-closed equivalent.
- hold clears: the same message is updated with a fresh `approval_view_id` and
  ENTER returns only if the fresh approval-time SafetyPolicy passes.
- BLOCKED / STALE / INVALIDATED / EXPIRED: the same message is updated and all
  action buttons are removed.
- human ENTER: intent is journaled, the message is closed, and it explicitly
  says no order was sent.
- human REJECT: rejection is journaled and the message is closed.

A stale Slack view never authorizes anything. On ENTER, Hermès checks that the
clicked view is the exact view Slack successfully displayed, then builds a fresh
current approval view on the market dispatch thread. The old
`approval_allowed_now=True` is never trusted. A valid ENTER still fails future
execution prerequisites with:

- `risk_eligibility_policy_not_implemented`
- `execution_layer_disabled`

Those are expected D1 safety blocks.

## Slack app configuration

Use **Socket Mode**. No public inbound HTTP endpoint is required.

Minimum scopes for this implementation:

- app-level token (`xapp-...`): `connections:write`
- bot token (`xoxb-...`): `chat:write`

Invite the app/bot to the target channel. The implementation intentionally does
not require `chat:write.public`, channel-list/read scopes, event subscriptions,
or file-upload scopes. Enable **Interactivity & Shortcuts** and **Socket Mode**
in the Slack app settings.

Required environment variables:

```bash
export SLACK_BOT_TOKEN='xoxb-...'
export SLACK_APP_TOKEN='xapp-...'
export HERMES_SLACK_CHANNEL_ID='C...'
```

Optional audit path:

```bash
export HERMES_SLACK_JOURNAL="$HOME/hermes-data/human_approvals.jsonl"
```

Do not paste tokens into chat, source files, Git history or logs. `.env` and
`.env.*` are ignored by Git (except `.env.example`).

Install the optional Slack dependency:

```bash
python -m pip install -e '.[slack]'
```

The project pins `slack-bolt==1.30.0` for this D1 checkpoint.

## Tests before real workspace connection

The unit tests use `FakeSlackTransport`; they do not require network access or
Slack credentials. Coverage includes:

- ACTIONABLE -> one post
- hold -> same message update, ENTER unavailable
- hold clear -> same message update, ENTER restored only if safe
- EXPIRED -> no buttons
- ENTER -> pure `HumanApprovalResponse`, no execution
- REJECT -> pure `HumanApprovalResponse`, no execution
- duplicate Slack delivery -> idempotent
- stale view -> fail closed
- Slack post failure -> market and decision state unchanged
- Slack package cannot import IBKR/order-path modules
- `hermes.decision` still cannot import Slack

Run:

```bash
python -m pytest -q
python -m pytest -q tests/unit/test_slack_approval.py tests/safety/test_slack_isolation.py
ruff check .
git diff --check
```

## Real Slack smoke test (after review)

1. Create/install the Slack app with the scopes above.
2. Invite the bot to the selected channel.
3. Export the three required environment variables.
4. Install the `slack` extra.
5. First test Slack itself (no TWS/IBKR involved):

```bash
python tools/slack_smoke.py
```

It posts one explicit "no order path" smoke message and closes.

6. Then start TWS/IB Gateway in the same READ-ONLY configuration used for C9
   and run Hermès normally:

```bash
python -m hermes.app.run_live --duration 300
```

If no ACTIONABLE candidate occurs during the smoke window, that is not a Slack
failure. The run summary exposes Slack connection/post/update/error counters.
For an end-to-end ENTER/REJECT test, wait for a real ACTIONABLE proposal; do not
weaken the C9 candidate gates just to force a Slack alert.

## Approver allowlist (D2.8)

`HERMES_SLACK_APPROVER_IDS` is a comma-separated list of Slack user IDs (`U…` / `W…`) allowed to
act on proposals.

- A click from any other user, or with a malformed id, changes nothing. It is journaled with
  `approver_not_allowed` / `approver_id_malformed`.
- Without the variable, ENTER can still be recorded as intent. The audit then carries
  `approver_allowlist_missing` and `approver_not_authorized_for_execution`.
- A malformed list disables Slack.
- Logs show only the number of approvers.

## Audit journal

Every processed button action records, without tokens:

- Slack interaction id and user id
- ENTER / REJECT
- setup_id / proposal_id
- clicked, displayed and current approval_view_id
- current lifecycle status and approval eligibility
- the pure `HumanApprovalResponse`
- stable result codes
- whether human intent was accepted
- `authorizes_execution=false`

Slack-generated ids never enter market or decision fingerprints.

## Screenshot extension point

`ApprovalPayload.screenshot_ref` remains the optional screenshot attachment
interface. D1 deliberately does not request Slack `files:write` and does not
block on screenshot generation. A future screenshot provider can be attached
outside the market hot path without changing the D1 safety model.
