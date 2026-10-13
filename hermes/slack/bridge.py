"""D1 Slack HUMAN_APPROVAL bridge.

The bridge is deliberately outside the deterministic decision core:

* decision -> ``publish_decision`` only enqueues an immutable view (no network);
* Slack button handlers ACK in the Slack thread and enqueue compact intent data;
* inbound intents are processed on the market dispatch thread in ``after_event``
  so the fresh SafetyPolicy check reads a single-writer engine state;
* Slack post/update and audit-file I/O happen on worker threads.

Outbox semantics (one Slack message per proposal):

* **latest state wins** — views of one proposal are coalesced while they wait for the worker, so a
  burst of lifecycle records costs one Slack update, never a backlog of stale alternating states;
* **transient holds are not displayed** — a VALID book is momentarily crossed / one-sided many
  times per minute (measured on recorded MNQ data: ~0.5 / s, median 0.2 ms). Each such moment is a
  correct TEMPORARY_HOLD for the decision core, but redrawing Slack for it made ENTER / REJECT
  flicker about once per second. A hold is shown only if it is still in effect after
  ``hold_display_delay_s``; a hold that cleared before then changes nothing visible. This is display
  only: ENTER is always re-checked against the live state on the dispatch thread (a click during a
  hold fails closed and the message is refreshed immediately);
* **the setup view owns the message after its window closed** — a later CLOSED decision view never
  overwrites the V1 Pro tracking view (it used to, depending on event order).

ENTER is human intent only.  There is no broker import and no execution path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from queue import Empty, Full, Queue, SimpleQueue
from threading import Lock, Thread

from hermes.decision.approval import ApprovalPayload, current_approval_payload
from hermes.decision.response import (
    HumanAction,
    HumanApprovalResponse,
    execution_prerequisites,
    response_matches_view,
)
from hermes.decision.runtime import JournalKind, JournalRecord
from hermes.market.events import ContractResolvedEvent
from hermes.slack.approvers import ApproverPolicy
from hermes.slack.chat import ConversationalWorker
from hermes.slack.enrichment import VisualEnrichmentWorker
from hermes.slack.journal import ApprovalAuditEntry, ApprovalJournal
from hermes.slack.pro import InterpretationWorker
from hermes.slack.protocol import (
    SlackAction,
    SlackInteraction,
    SlackMessageRef,
    SlackTransport,
    SlackWorkflowState,
)
from hermes.slack.render import render_setup_message, render_slack_message
from hermes.slack.summary import contract_roll_warning, setup_context_text

log = logging.getLogger("hermes.slack.bridge")

_STOP = object()
_RELEVANT = frozenset(
    {
        JournalKind.APPROVAL_VIEW_CREATED,
        JournalKind.TEMPORARY_HOLD_ENTERED,
        JournalKind.TEMPORARY_HOLD_CLEARED,
        JournalKind.CANDIDATE_BLOCKED,
        JournalKind.CANDIDATE_STALE,
        JournalKind.CANDIDATE_INVALIDATED,
        JournalKind.CANDIDATE_EXPIRED,
    }
)
_TERMINAL = frozenset({"BLOCKED", "STALE", "INVALIDATED", "EXPIRED", "NONE"})


HOLD_DISPLAY_DELAY_S = 1.0          # a temporary hold shorter than this is never drawn in Slack


@dataclass(frozen=True, slots=True)
class OutboundView:
    view: ApprovalPayload
    state: SlackWorkflowState
    banner: str | None = None
    hold: str = ""                   # "entered" / "cleared" for TEMPORARY_HOLD_* records (display debounce)
    at: float = 0.0                  # monotonic enqueue time
    roll_warning: str | None = None  # contract near/after expiry (display only)


@dataclass(frozen=True, slots=True)
class _ViewToken:
    proposal_id: str


@dataclass(frozen=True, slots=True)
class OutboundSetup:
    """V1 Pro: update of an existing proposal message with the setup-tracking view (no buttons)."""
    proposal_id: str
    view: object                     # hermes.decision.tracking.SetupView (immutable)
    kind: str                        # WINDOW_CLOSED / REARMED / INVALIDATED / OUTCOME_FINAL


SETUP_SLACK_EVENTS = frozenset({"WINDOW_CLOSED", "REARMED", "INVALIDATED", "OUTCOME_FINAL"})


@dataclass(slots=True)
class SlackBridgeStats:
    queued: int = 0
    outbox_dropped: int = 0
    posts: int = 0
    updates: int = 0
    post_failures: int = 0
    update_failures: int = 0
    connect_failures: int = 0
    interactions_received: int = 0
    interactions_processed: int = 0
    duplicates: int = 0
    accepted_enters: int = 0
    rejects: int = 0
    unauthorized: int = 0
    setup_updates: int = 0
    coalesced: int = 0               # views replaced by a newer state before delivery
    holds_deferred: int = 0          # holds waiting for hold_display_delay_s
    holds_suppressed: int = 0        # transient holds never drawn (cleared in time)
    superseded: int = 0              # CLOSED views not drawn over the setup-tracking view

    def as_dict(self) -> dict[str, int]:
        return {
            "queued": self.queued,
            "outbox_dropped": self.outbox_dropped,
            "posts": self.posts,
            "updates": self.updates,
            "post_failures": self.post_failures,
            "update_failures": self.update_failures,
            "connect_failures": self.connect_failures,
            "interactions_received": self.interactions_received,
            "interactions_processed": self.interactions_processed,
            "duplicates": self.duplicates,
            "accepted_enters": self.accepted_enters,
            "rejects": self.rejects,
            "unauthorized": self.unauthorized,
            "setup_updates": self.setup_updates,
            "coalesced": self.coalesced,
            "holds_deferred": self.holds_deferred,
            "holds_suppressed": self.holds_suppressed,
            "superseded": self.superseded,
        }


def _uniq(codes) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(x) for x in codes if x))


def _response_dict(resp: HumanApprovalResponse) -> dict:
    return {
        "schema_version": resp.schema_version,
        "action": resp.action.value,
        "setup_id": resp.setup_id,
        "proposal_id": resp.proposal_id,
        "approval_view_id": resp.approval_view_id,
        "response_wall_ns": resp.response_wall_ns,
        "response_seq": resp.response_seq,
        "note": resp.note,
        "authorizes_execution": resp.authorizes_execution,
    }


def _state_for_view(p: ApprovalPayload) -> SlackWorkflowState:
    if p.status in _TERMINAL:
        return SlackWorkflowState.CLOSED
    if p.status == "ACTIONABLE" and not p.approval_allowed_now:
        return SlackWorkflowState.HOLD
    return SlackWorkflowState.ACTIVE


class SlackApprovalBridge:
    """Non-blocking Slack adapter around the existing C9 approval model."""

    def __init__(
        self,
        runtime,
        engine,
        cfg,
        transport: SlackTransport,
        journal: ApprovalJournal | None = None,
        *,
        market_context_provider=None,
        status_provider=None,
        max_outbox: int = 256,
        max_interactions_per_event: int = 16,
        approvers: ApproverPolicy | None = None,
        history_provider=None,
        note_provider=None,
        setup_interpreter=None,
        hold_display_delay_s: float = HOLD_DISPLAY_DELAY_S,
        clock=time.monotonic,
    ) -> None:
        self.runtime = runtime
        self.hold_display_delay_s = max(0.0, float(hold_display_delay_s))
        self._clock = clock
        self.engine = engine
        self.cfg = cfg
        self.transport = transport
        self.journal = journal or ApprovalJournal()
        self.approvers = approvers or ApproverPolicy(None)   # None = allowlist missing (fail closed for execution)
        # V1 Pro read-only providers (called on the outbox thread at render time; never gate anything):
        self.history_provider = history_provider             # ApprovalPayload -> history stat | None (>= 30 only)
        self.note_provider = note_provider                   # ApprovalPayload -> one-line re-arm note | None
        self._human_actions: dict[str, str] = {}
        # D8: optional LLM interpretation of setup transitions, posted as a thread reply (display only)
        self.interpretation = InterpretationWorker(transport, setup_interpreter) if setup_interpreter else None
        self.enrichment = VisualEnrichmentWorker(transport)

        if market_context_provider is None:
            def market_context_provider() -> str:
                return "Hermès market context unavailable."

        self.chat = ConversationalWorker(
            transport,
            market_context_provider,
            status_provider=status_provider,
            setup_context_provider=self.setup_context,
            thread_user_allowed=lambda user_id: self.approvers.check(user_id).may_act,
        )

        if hasattr(transport, "set_mention_handler"):
            transport.set_mention_handler(self.chat.submit)
        if hasattr(transport, "set_thread_message_handler"):
            transport.set_thread_message_handler(self.chat.submit_thread_message)
        self.max_interactions_per_event = max_interactions_per_event

        self.stats = SlackBridgeStats()
        self._inbox: SimpleQueue[SlackInteraction] = SimpleQueue()
        self._outbox: Queue[object] = Queue(maxsize=max_outbox)
        self._worker: Thread | None = None
        self._started = False

        self._display_lock = Lock()
        self._message_refs: dict[str, SlackMessageRef] = {}
        self._displayed: dict[str, ApprovalPayload] = {}

        # outbox state: _pending is shared (publisher thread -> worker); the rest is worker-thread only
        self._pending_lock = Lock()
        self._pending: dict[str, OutboundView] = {}
        self._deferred: dict[str, tuple[float, OutboundView]] = {}
        self._hold_since: dict[str, float] = {}
        self._hold_shown: set[str] = set()
        self._setup_views: dict[str, OutboundSetup] = {}
        self._latest_setup: dict[str, object] = {}          # proposal_id -> latest tracking SetupView (immutable)

        self._contract: tuple[str, str] | None = None     # (local_symbol, expiry) of the LIVE resolved contract
        self._roll_logged: str | None = None

        self._seen_interactions: set[str] = set()
        self._seen_responses: set[tuple[str, str, str, str, str]] = set()
        self._human_closed: set[str] = set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.journal.start()
        log.info("Slack TWS screenshot %s (HERMES_SCREENSHOT_ENABLED); posted as a thread reply",
                 "enabled" if self.enrichment.enabled else "DISABLED")
        self.enrichment.start()
        self.chat.start()
        if self.interpretation is not None:
            self.interpretation.start()
        self._worker = Thread(target=self._run_outbox, name="hermes-slack-outbox", daemon=True)
        self._worker.start()
        try:
            self.transport.start(self._receive_interaction)
        except Exception as exc:  # noqa: BLE001
            self.stats.connect_failures += 1
            log.error("Slack transport start failed; core continues without Slack: %s", exc)

    def close(self, timeout: float = 3.0) -> None:
        if not self._started:
            return
        try:
            self.transport.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Slack transport close failed: %s", exc)
        try:
            self._outbox.put(_STOP, timeout=0.2)
        except Full:
            log.warning("Slack outbox full during shutdown; worker will be left daemonized")
        if self._worker is not None:
            self._worker.join(timeout)
            if self._worker.is_alive():
                log.warning("Slack outbox worker did not stop within %.1fs", timeout)
        self.chat.close()
        self.enrichment.close()
        if self.interpretation is not None:
            self.interpretation.close(timeout)
        self.journal.close(timeout)
        self._worker = None
        self._started = False

    def publish_decision(self, record: JournalRecord, view: ApprovalPayload | None) -> None:
        """Queue one meaningful decision transition; never performs network I/O."""
        if record.kind not in _RELEVANT or view is None:
            return
        if view.proposal_id in self._human_closed:
            return
        hold = ""
        if record.kind is JournalKind.TEMPORARY_HOLD_ENTERED:
            state, banner = SlackWorkflowState.HOLD, "Temporary market-data hold — ENTER unavailable."
            hold = "entered"
        elif record.kind is JournalKind.TEMPORARY_HOLD_CLEARED:
            state, banner = _state_for_view(view), "Temporary hold cleared; fresh approval view shown."
            hold = "cleared"
        elif record.kind in {
            JournalKind.CANDIDATE_BLOCKED,
            JournalKind.CANDIDATE_STALE,
            JournalKind.CANDIDATE_INVALIDATED,
            JournalKind.CANDIDATE_EXPIRED,
        }:
            state, banner = SlackWorkflowState.CLOSED, f"Candidate {record.status or 'closed'} — ENTER disabled."
        else:
            state, banner = _state_for_view(view), None
        self._enqueue(OutboundView(view, state, banner, hold=hold, at=self._clock(),
                                   roll_warning=self._roll_warning(record.wall_ns)))

    def _roll_warning(self, wall_ns: int) -> str | None:
        if self._contract is None:
            return None
        text = contract_roll_warning(self._contract[0], self._contract[1], wall_ns)
        if text and text != self._roll_logged:
            self._roll_logged = text
            log.warning("%s", text)
        return text

    def publish_setup(self, event, view) -> None:
        """V1 Pro: queue a setup-tracking update of the proposal's existing message. Never posts a new
        message, never carries buttons, never performs network I/O on the caller's thread."""
        pid = getattr(view, "proposal_id", None)
        if pid:
            with self._display_lock:
                self._latest_setup[pid] = view                # conversation context (read on the chat thread)
        if event.kind not in SETUP_SLACK_EVENTS or not pid:
            return
        with self._display_lock:
            known = pid in self._message_refs or pid in self._displayed
        if not known and event.kind != "WINDOW_CLOSED":
            return
        self._enqueue(OutboundSetup(pid, view, event.kind))

    def _receive_interaction(self, interaction: SlackInteraction) -> None:
        self.stats.interactions_received += 1
        self._inbox.put(interaction)

    def after_event(self, raw, events, now_mono_ns: int) -> None:
        """Drain a bounded number of Slack intents on the single-writer dispatch thread."""
        for ev in events:
            if isinstance(ev, ContractResolvedEvent):
                self._contract = (ev.local_symbol, ev.expiry)        # display-only roll warning source
        for _ in range(self.max_interactions_per_event):
            try:
                interaction = self._inbox.get_nowait()
            except Empty:
                return
            self._process_interaction(interaction, raw)

    def _process_interaction(self, i: SlackInteraction, raw) -> None:
        response_key = (i.user_id, i.action.value, i.setup_id, i.proposal_id, i.approval_view_id)
        if i.interaction_id in self._seen_interactions or response_key in self._seen_responses:
            self.stats.duplicates += 1
            return
        self._seen_interactions.add(i.interaction_id)
        self._seen_responses.add(response_key)
        self.stats.interactions_processed += 1

        codes: list[str] = []
        response: HumanApprovalResponse | None = None
        accepted = False
        current: ApprovalPayload | None = None

        approver = self.approvers.check(i.user_id)
        codes.append(approver.value)
        if not approver.may_act:
            # D2.8 fail closed: an unlisted / malformed identity changes nothing; audited only.
            self.stats.unauthorized += 1
            codes.append("enter_failed_closed" if i.action is SlackAction.ENTER else "reject_not_recorded")
            self.journal.append(self._audit(i, raw, None, None, None, codes, accepted=False))
            log.warning("Slack %s ignored: %s", i.action.value, approver.value)
            return

        with self._display_lock:
            displayed = self._displayed.get(i.proposal_id)

        rec = self.runtime.driver.lifecycle.get(i.setup_id)
        if rec is None:
            codes.append("unknown_setup_id")
        else:
            try:
                current = current_approval_payload(
                    self.engine,
                    rec,
                    self.cfg,
                    now_wall_ns=raw.recv_wall_ns,
                    instrument_id=rec.candidate.instrument_id,
                )
            except Exception:
                codes.append("current_approval_view_failed")
                log.exception("could not build fresh approval view for Slack interaction")

        try:
            response = HumanApprovalResponse(
                action=HumanAction(i.action.value),
                setup_id=i.setup_id,
                proposal_id=i.proposal_id,
                approval_view_id=i.approval_view_id,
                response_wall_ns=raw.recv_wall_ns,
                response_seq=raw.seq,
            )
        except (TypeError, ValueError):
            codes.append("invalid_interaction_payload")

        displayed_matches = (
            displayed is not None
            and displayed.setup_id == i.setup_id
            and displayed.proposal_id == i.proposal_id
            and displayed.approval_view_id == i.approval_view_id
        )
        if displayed is None:
            codes.append("slack_view_not_displayed")
        elif not displayed_matches:
            codes.append("slack_view_stale")

        if response is not None and displayed_matches:
            ok, reasons = response_matches_view(response, displayed)
            if not ok:
                codes.extend(r.code for r in reasons)

        if i.proposal_id in self._human_closed:
            codes.append("human_workflow_closed")

        if current is not None and current.proposal_id != i.proposal_id:
            codes.append("proposal_changed")

        if i.action is SlackAction.REJECT:
            if (
                response is not None
                and displayed is not None
                and current is not None
                and current.proposal_id == i.proposal_id
                and i.proposal_id not in self._human_closed
            ):
                accepted = True
                self._human_closed.add(i.proposal_id)
                self._human_actions[i.proposal_id] = "REJECT recorded"
                self.stats.rejects += 1
                self._enqueue(
                    OutboundView(
                        current,
                        SlackWorkflowState.REJECTED,
                        "REJECT recorded. No order was sent.",
                    )
                )
            else:
                codes.append("reject_not_recorded")
        else:
            enter_ok = (
                response is not None
                and displayed_matches
                and current is not None
                and current.proposal_id == i.proposal_id
                and i.proposal_id not in self._human_closed
                and current.status == "ACTIONABLE"
                and current.approval_allowed_now
            )
            if enter_ok:
                prereq = execution_prerequisites(response, rec, current.approval_safety)
                codes.extend(prereq.codes)
                if not approver.may_authorize_execution:
                    codes.append("approver_not_authorized_for_execution")
                accepted = True
                self._human_closed.add(i.proposal_id)
                self._human_actions[i.proposal_id] = "ENTER intent recorded (no order sent)"
                self.stats.accepted_enters += 1
                self._enqueue(
                    OutboundView(
                        current,
                        SlackWorkflowState.ENTER_RECORDED,
                        "ENTER intent recorded. No order was sent; execution remains disabled.",
                    )
                )
            else:
                codes.append("enter_failed_closed")
                if current is not None:
                    codes.extend(r.code for r in current.denied_reasons)
                    self._enqueue(
                        OutboundView(
                            current,
                            _state_for_view(current),
                            "Market state changed or the Slack view is no longer current. Re-review before ENTER.",
                        )
                    )

        self.journal.append(self._audit(i, raw, response, displayed, current, codes, accepted=accepted))

    @staticmethod
    def _audit(i: SlackInteraction, raw, response, displayed, current, codes, *, accepted: bool) -> ApprovalAuditEntry:
        return ApprovalAuditEntry(
            interaction_id=i.interaction_id,
            slack_user_id=i.user_id,
            action=i.action.value,
            interaction_wall_ns=i.interaction_wall_ns,
            processed_wall_ns=raw.recv_wall_ns,
            processed_seq=raw.seq,
            setup_id=i.setup_id,
            proposal_id=i.proposal_id,
            clicked_approval_view_id=i.approval_view_id,
            displayed_approval_view_id=displayed.approval_view_id if displayed else None,
            current_approval_view_id=current.approval_view_id if current else None,
            current_status=current.status if current else None,
            current_approval_allowed_now=bool(current and current.approval_allowed_now),
            human_response=_response_dict(response) if response else None,
            result_codes=_uniq(codes),
            accepted_human_intent=accepted,
            authorizes_execution=False,
        )

    def _enqueue(self, item) -> None:
        """Never blocks. Views of one proposal coalesce (latest state wins); setup updates keep order."""
        if isinstance(item, OutboundView):
            pid = item.view.proposal_id
            with self._pending_lock:
                waiting = pid in self._pending
                self._pending[pid] = item
            if waiting:
                self.stats.coalesced += 1
                self.stats.queued += 1
                return
            token: object = _ViewToken(pid)
        else:
            token = item
        try:
            self._outbox.put_nowait(token)
            self.stats.queued += 1
        except Full:
            if isinstance(token, _ViewToken):
                with self._pending_lock:
                    self._pending.pop(token.proposal_id, None)
            self.stats.outbox_dropped += 1
            log.error(
                "Slack outbox full; dropped update for proposal %s (market/decision core continues)",
                getattr(item, "proposal_id", None) or item.view.proposal_id,
            )

    def _run_outbox(self) -> None:
        while True:
            timeout = self._next_due_in()
            try:
                item = self._outbox.get() if timeout is None else self._outbox.get(timeout=timeout)
            except Empty:
                self._flush_due()
                continue
            try:
                if item is _STOP:
                    return
                if isinstance(item, OutboundSetup):
                    self._deliver_setup(item)
                elif isinstance(item, _ViewToken):
                    with self._pending_lock:
                        view = self._pending.pop(item.proposal_id, None)
                    if view is not None:
                        self._consider(view)
                self._flush_due()
            finally:
                self._outbox.task_done()

    def _next_due_in(self) -> float | None:
        if not self._deferred:
            return None
        return max(0.0, min(due for due, _ in self._deferred.values()) - self._clock())

    def _flush_due(self) -> None:
        now = self._clock()
        for pid, (due, item) in list(self._deferred.items()):
            if now >= due:
                del self._deferred[pid]
                self._show(item)

    def _consider(self, item: OutboundView) -> None:
        """Worker thread: decide whether this (latest) view changes what Slack must show."""
        pid = item.view.proposal_id
        self._deferred.pop(pid, None)                     # a newer state supersedes a waiting hold
        setup = self._setup_views.get(pid)
        if setup is not None:
            # the message now shows the setup-tracking view: never redraw the closed proposal over it
            if item.state is SlackWorkflowState.CLOSED:
                self.stats.superseded += 1
            else:
                self._deliver_setup(setup, interpret=False)   # re-render with the human action
            return
        if item.hold == "entered" and item.state is SlackWorkflowState.HOLD:
            since = self._hold_since.setdefault(pid, item.at)
            due = since + self.hold_display_delay_s
            if pid not in self._hold_shown and self._clock() < due:
                self._deferred[pid] = (due, item)
                self.stats.holds_deferred += 1
                return
            self._show(item)
            return
        self._hold_since.pop(pid, None)
        if item.hold == "cleared" and pid not in self._hold_shown:
            with self._display_lock:
                posted = pid in self._message_refs
            if posted:
                self.stats.holds_suppressed += 1          # the hold was never drawn: nothing changed
                return
            item = replace(item, banner=None)
        self._show(item)

    def _show(self, item: OutboundView) -> None:
        pid = item.view.proposal_id
        if self._deliver(item):
            if item.state is SlackWorkflowState.HOLD:
                self._hold_shown.add(pid)
            else:
                self._hold_shown.discard(pid)
                self._hold_since.pop(pid, None)

    def _deliver_setup(self, item: OutboundSetup, interpret: bool = True) -> None:
        with self._display_lock:
            ref = self._message_refs.get(item.proposal_id)
        if ref is None:
            return                                   # no message of ours to update (never post a stray one)
        msg = render_setup_message(item.view, item.kind, human=self._human_actions.get(item.proposal_id))
        try:
            self.transport.update(ref, msg)
        except Exception as exc:  # noqa: BLE001
            self.stats.update_failures += 1
            log.error("Slack setup update failed for %s: %s", item.proposal_id, exc)
            return
        self.stats.setup_updates += 1
        self._setup_views[item.proposal_id] = item
        self._deferred.pop(item.proposal_id, None)
        if interpret and self.interpretation is not None and item.kind in ("WINDOW_CLOSED", "REARMED"):
            self.interpretation.submit(ref, item.view)

    def _provided(self, provider, p):
        if provider is None:
            return None
        try:
            return provider(p)
        except Exception:  # never let analytics break the approval message
            log.exception("Slack provider failed; message rendered without it")
            return None

    def _deliver(self, item: OutboundView) -> bool:
        p = item.view
        fresh = item.state is SlackWorkflowState.ACTIVE
        msg = render_slack_message(p, workflow_state=item.state, banner=item.banner,
                                   history=self._provided(self.history_provider, p) if fresh else None,
                                   note=self._provided(self.note_provider, p) if fresh else None,
                                   roll_warning=item.roll_warning)
        with self._display_lock:
            ref = self._message_refs.get(p.proposal_id)
        if ref is None:
            try:
                new_ref = self.transport.post(msg)
            except Exception as exc:  # noqa: BLE001
                self.stats.post_failures += 1
                log.error("Slack post failed for %s: %s", p.proposal_id, exc)
                return False
            with self._display_lock:
                self._message_refs[p.proposal_id] = new_ref
                self._displayed[p.proposal_id] = p
            self.stats.posts += 1

            # The screenshot is visual context only (it authorizes nothing): take it for every proposal
            # posted while ACTIONABLE, even if a transient hold happened to be in effect at that instant.
            if p.status == "ACTIONABLE":
                self.enrichment.submit(p, new_ref)
            else:
                log.info("Slack screenshot skipped for %s: posted as %s", p.proposal_id, p.status)
            return True
        try:
            self.transport.update(ref, msg)
        except Exception as exc:  # noqa: BLE001
            self.stats.update_failures += 1
            log.error("Slack update failed for %s: %s", p.proposal_id, exc)
            return False
        with self._display_lock:
            self._displayed[p.proposal_id] = p
        self.stats.updates += 1
        return True

    def setup_context(self, channel_id: str, thread_ts: str) -> str | None:
        """Facts of the Hermès proposal whose message started this thread (None if it is not one).
        Read on the chat thread from immutable snapshots only; never touches the engine."""
        with self._display_lock:
            pid = next((p for p, ref in self._message_refs.items()
                        if ref.ts == thread_ts and ref.channel_id == channel_id), None)
            if pid is None:
                return None
            order = list(self._message_refs)
            payload = self._displayed.get(pid)
            view = self._latest_setup.get(pid)
            prev_pid = order[order.index(pid) - 1] if order.index(pid) > 0 else None
            prev = self._displayed.get(prev_pid) if prev_pid else None
            prev_view = self._latest_setup.get(prev_pid) if prev_pid else None
        return setup_context_text(payload, view, self._human_actions.get(pid), prev, prev_view)

    def displayed_view(self, proposal_id: str) -> ApprovalPayload | None:
        with self._display_lock:
            return self._displayed.get(proposal_id)

    def message_ref(self, proposal_id: str) -> SlackMessageRef | None:
        with self._display_lock:
            return self._message_refs.get(proposal_id)

    def summary(self) -> dict:
        out = self.stats.as_dict()
        out.update(
            {
                "enabled": True,
                "channel_id": self.transport.channel_id,
                "journal_entries": self.journal.count,
                "journal_write_errors": self.journal.write_errors,
                "journal_dropped": self.journal.dropped,
                "human_closed": len(self._human_closed),
                "visual_enrichment": self.enrichment.summary(),
                "interpretation": self.interpretation.summary() if self.interpretation is not None else None,
                "conversation": self.chat.summary(),
            }
        )
        return out
