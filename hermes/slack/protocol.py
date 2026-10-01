"""D1 Slack HUMAN_APPROVAL transport contracts.

This package is intentionally outside :mod:`hermes.decision`.  The deterministic
market/decision core never imports Slack and never waits on network I/O.

Only compact proposal/view identities cross the interactive button boundary.  A
Slack click is human intent data; it can never authorize broker execution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

ENTER_ACTION_ID = "hermes_enter"
REJECT_ACTION_ID = "hermes_reject"


class SlackAction(str, Enum):
    ENTER = "ENTER"
    REJECT = "REJECT"


class SlackWorkflowState(str, Enum):
    ACTIVE = "ACTIVE"
    HOLD = "HOLD"
    ENTER_RECORDED = "ENTER_RECORDED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class SlackMessage:
    text: str
    blocks: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class SlackMessageRef:
    channel_id: str
    ts: str


@dataclass(frozen=True, slots=True)
class SlackInteraction:
    """A compact, already-acknowledged Slack button interaction.

    ``interaction_id`` is transport/idempotency metadata only.  It never enters
    market or decision fingerprints.
    """

    interaction_id: str
    user_id: str
    action: SlackAction
    setup_id: str
    proposal_id: str
    approval_view_id: str
    interaction_wall_ns: int | None = None


InteractionHandler = Callable[[SlackInteraction], None]


class SlackTransport(Protocol):
    """Network boundary used by :class:`SlackApprovalBridge`.

    Implementations may block in ``post``/``update``; those methods are called
    only on the Slack outbox worker, never on the market-data dispatch thread.
    """

    channel_id: str

    def start(self, on_interaction: InteractionHandler) -> None: ...
    def close(self) -> None: ...
    def post(self, message: SlackMessage) -> SlackMessageRef: ...
    def update(self, ref: SlackMessageRef, message: SlackMessage) -> None: ...
    def upload_file(
        self,
        ref: SlackMessageRef,
        path: str,
        *,
        title: str,
        initial_comment: str,
    ) -> None: ...
