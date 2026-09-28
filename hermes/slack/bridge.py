"""D1 Slack HUMAN_APPROVAL bridge.

The bridge is deliberately outside the deterministic decision core:

* decision -> ``publish_decision`` only enqueues an immutable view (no network);
* Slack button handlers ACK in the Slack thread and enqueue compact intent data;
* inbound intents are processed on the market dispatch thread in ``after_event``
  so the fresh SafetyPolicy check reads a single-writer engine state;
* Slack post/update and audit-file I/O happen on worker threads.

ENTER is human intent only.  There is no broker import and no execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
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
from hermes.slack.journal import ApprovalAuditEntry, ApprovalJournal
from hermes.slack.protocol import (
    SlackAction,
    SlackInteraction,
    SlackMessageRef,
    SlackTransport,
    SlackWorkflowState,
)
from hermes.slack.render import render_slack_message

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


@dataclass(frozen=True, slots=True)
class OutboundView:
    view: ApprovalPayload
    state: SlackWorkflowState
    banner: str | None = None


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
        max_outbox: int = 256,
        max_interactions_per_event: int = 16,
    ) -> None:
        self.runtime = runtime
        self.engine = engine
        self.cfg = cfg
        self.transport = transport
        self.journal = journal or ApprovalJournal()
        self.max_interactions_per_event = max_interactions_per_event

        self.stats = SlackBridgeStats()
        self._inbox: SimpleQueue[SlackInteraction] = SimpleQueue()
        self._outbox: Queue[object] = Queue(maxsize=max_outbox)
        self._worker: Thread | None = None
        self._started = False

        self._display_lock = Lock()
        self._message_refs: dict[str, SlackMessageRef] = {}
        self._displayed: dict[str, ApprovalPayload] = {}

        self._seen_interactions: set[str] = set()
        self._seen_responses: set[tuple[str, str, str, str, str]] = set()
        self._human_closed: set[str] = set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.journal.start()
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
        self.journal.close(timeout)
        self._worker = None
        self._started = False

    def publish_decision(self, record: JournalRecord, view: ApprovalPayload | None) -> None:
        """Queue one meaningful decision transition; never performs network I/O."""
        if record.kind not in _RELEVANT or view is None:
            return
        if view.proposal_id in self._human_closed:
            return
        if record.kind is JournalKind.TEMPORARY_HOLD_ENTERED:
            state, banner = SlackWorkflowState.HOLD, "Temporary market-data hold — ENTER unavailable."
        elif record.kind is JournalKind.TEMPORARY_HOLD_CLEARED:
            state, banner = _state_for_view(view), "Temporary hold cleared; fresh approval view shown."
        elif record.kind in {
            JournalKind.CANDIDATE_BLOCKED,
            JournalKind.CANDIDATE_STALE,
            JournalKind.CANDIDATE_INVALIDATED,
            JournalKind.CANDIDATE_EXPIRED,
        }:
            state, banner = SlackWorkflowState.CLOSED, f"Candidate {record.status or 'closed'} — ENTER disabled."
        else:
            state, banner = _state_for_view(view), None
        self._enqueue(OutboundView(view, state, banner))

    def _receive_interaction(self, interaction: SlackInteraction) -> None:
        self.stats.interactions_received += 1
        self._inbox.put(interaction)

    def after_event(self, raw, events, now_mono_ns: int) -> None:  # noqa: ARG002
        """Drain a bounded number of Slack intents on the single-writer dispatch thread."""
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
            except Exception as exc:  # noqa: BLE001
                codes.append("current_approval_view_failed")
                log.exception("could not build fresh approval view for Slack interaction: %s", exc)

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
                accepted = True
                self._human_closed.add(i.proposal_id)
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

        entry = ApprovalAuditEntry(
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
        self.journal.append(entry)

    def _enqueue(self, item: OutboundView) -> None:
        try:
            self._outbox.put_nowait(item)
            self.stats.queued += 1
        except Full:
            self.stats.outbox_dropped += 1
            log.error(
                "Slack outbox full; dropped update for proposal %s (market/decision core continues)",
                item.view.proposal_id,
            )

    def _run_outbox(self) -> None:
        while True:
            item = self._outbox.get()
            try:
                if item is _STOP:
                    return
                assert isinstance(item, OutboundView)
                self._deliver(item)
            finally:
                self._outbox.task_done()

    def _deliver(self, item: OutboundView) -> None:
        p = item.view
        msg = render_slack_message(p, workflow_state=item.state, banner=item.banner)
        with self._display_lock:
            ref = self._message_refs.get(p.proposal_id)
        if ref is None:
            try:
                new_ref = self.transport.post(msg)
            except Exception as exc:  # noqa: BLE001
                self.stats.post_failures += 1
                log.error("Slack post failed for %s: %s", p.proposal_id, exc)
                return
            with self._display_lock:
                self._message_refs[p.proposal_id] = new_ref
                self._displayed[p.proposal_id] = p
            self.stats.posts += 1
            return
        try:
            self.transport.update(ref, msg)
        except Exception as exc:  # noqa: BLE001
            self.stats.update_failures += 1
            log.error("Slack update failed for %s: %s", p.proposal_id, exc)
            return
        with self._display_lock:
            self._displayed[p.proposal_id] = p
        self.stats.updates += 1

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
            }
        )
        return out
