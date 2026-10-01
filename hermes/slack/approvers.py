"""Slack approver allowlist (D2.8).

Who may act on a Hermès proposal in Slack. Today a Slack ENTER is human intent only (nothing is
ever executed), but the identity check must exist and be journaled before any future execution
phase. Configuration: ``HERMES_SLACK_APPROVER_IDS`` = comma-separated Slack user IDs
(``U…`` / ``W…``). No ID is ever invented or defaulted here.

Rules (fail closed):
- allowlist configured, user listed      -> AUTHORIZED: ENTER / REJECT handled as before;
- allowlist configured, user not listed  -> NOT_ALLOWED: the click changes nothing, it is audited;
- malformed user id                      -> MALFORMED_USER: same as NOT_ALLOWED;
- allowlist missing                      -> ALLOWLIST_MISSING: REJECT may close the workflow and an
  ENTER may still be recorded as intent (Phase D1 behavior), but it carries
  ``approver_allowlist_missing`` and can never satisfy an execution prerequisite.
- a malformed allowlist is a configuration error (Slack is disabled), never a partial allowlist.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

APPROVER_ENV = "HERMES_SLACK_APPROVER_IDS"
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,30}$")


class ApproverConfigError(ValueError):
    """The approver allowlist is present but malformed."""


class ApproverStatus(str, Enum):
    AUTHORIZED = "approver_authorized"
    NOT_ALLOWED = "approver_not_allowed"
    MALFORMED_USER = "approver_id_malformed"
    ALLOWLIST_MISSING = "approver_allowlist_missing"

    @property
    def may_act(self) -> bool:
        """May this click change the human workflow at all (record intent / close it)?"""
        return self in (ApproverStatus.AUTHORIZED, ApproverStatus.ALLOWLIST_MISSING)

    @property
    def may_authorize_execution(self) -> bool:
        """Necessary (never sufficient) condition for any future execution layer."""
        return self is ApproverStatus.AUTHORIZED


def valid_user_id(user_id: object) -> bool:
    return isinstance(user_id, str) and bool(_SLACK_USER_ID.match(user_id))


@dataclass(frozen=True, slots=True)
class ApproverPolicy:
    approver_ids: frozenset[str] | None = None        # None = allowlist not configured

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApproverPolicy:
        env = os.environ if env is None else env
        raw = (env.get(APPROVER_ENV) or "").strip()
        if not raw:
            return cls(None)
        ids = [x.strip() for x in raw.split(",")]
        bad = [x for x in ids if not valid_user_id(x)]
        if bad or not ids:
            raise ApproverConfigError(
                f"{APPROVER_ENV} must be a comma-separated list of Slack user IDs (U…/W…); "
                f"{len(bad)} invalid entr{'y' if len(bad) == 1 else 'ies'}")
        return cls(frozenset(ids))

    @property
    def configured(self) -> bool:
        return self.approver_ids is not None

    def check(self, user_id: object) -> ApproverStatus:
        if not valid_user_id(user_id):
            return ApproverStatus.MALFORMED_USER
        if self.approver_ids is None:
            return ApproverStatus.ALLOWLIST_MISSING
        return ApproverStatus.AUTHORIZED if user_id in self.approver_ids else ApproverStatus.NOT_ALLOWED

    def describe(self) -> str:
        """Safe for logs: counts only, never the IDs."""
        return "not configured" if self.approver_ids is None else f"{len(self.approver_ids)} approver(s)"
