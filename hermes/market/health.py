"""Stream / subscription health state and the bounded 10197 recovery state machine.

Principles (C3 amendment B)
---------------------------
* Stream AGE is telemetry, never by itself a failure. A quiet BBO or book can be correct.
* Health decisions combine evidence: connection state, data-farm state, subscription errors,
  marketDataType, cross-stream consistency (book vs BBO), recovery state.
* Time may FAIL a recovery attempt; it never PROVES recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hermes.market.events import Stream, StreamStatus


@dataclass(slots=True)
class StreamState:
    stream: Stream
    generation: int | None = None          # ACTIVE reqId (None = no active subscription)
    status: StreamStatus = StreamStatus.IDLE
    requested_seq: int | None = None
    requested_mono_ns: int | None = None
    first_data_mono_ns: int | None = None  # first event of the active generation
    last_event_mono_ns: int | None = None  # telemetry (age), NOT a failure criterion
    events: int = 0                        # events of the active generation
    error_active: bool = False             # fatal error on the active generation
    last_error_code: int | None = None
    requests: int = 0                      # generations requested so far

    def on_requested(self, generation: int, seq: int, mono_ns: int) -> None:
        self.generation = generation
        self.status = StreamStatus.REQUESTED
        self.requested_seq = seq
        self.requested_mono_ns = mono_ns
        self.first_data_mono_ns = None
        self.events = 0
        self.error_active = False
        self.last_error_code = None
        self.requests += 1

    def on_cancelled(self, generation: int) -> None:
        if self.generation == generation:
            self.generation = None
            self.status = StreamStatus.CANCELLED

    def on_data(self, mono_ns: int) -> None:
        if self.first_data_mono_ns is None:
            self.first_data_mono_ns = mono_ns
        self.last_event_mono_ns = mono_ns
        self.events += 1
        if self.status is StreamStatus.REQUESTED and not self.error_active:
            self.status = StreamStatus.ACTIVE

    def on_fatal_error(self, code: int) -> None:
        self.error_active = True
        self.last_error_code = code
        self.status = StreamStatus.UNAVAILABLE

    def age_ns(self, now_mono_ns: int) -> int | None:
        return None if self.last_event_mono_ns is None else now_mono_ns - self.last_event_mono_ns


class ConflictPhase(Enum):
    NONE = "none"              # no market-data session conflict
    BLOCKED = "blocked"        # conflict active; waiting for a (paced) recovery attempt
    RECOVERING = "recovering"  # resubscription attempt in progress; must PROVE recovery
    EXHAUSTED = "exhausted"    # retry budget exhausted: market data UNAVAILABLE, no automatic retries


@dataclass(slots=True)
class ConflictRecovery:
    """Error 10197 (market-data session conflict) state. Deterministic: driven by events only."""

    max_attempts: int
    attempt_timeout_ns: int
    phase: ConflictPhase = ConflictPhase.NONE
    attempts: int = 0                      # consecutive failed/started attempts in this budget
    attempt_started_seq: int | None = None
    attempt_started_mono_ns: int | None = None
    last_conflict_seq: int | None = None
    conflicts_seen: int = 0
    recoveries: int = 0
    failures: int = 0

    @property
    def active(self) -> bool:
        return self.phase is not ConflictPhase.NONE

    def on_conflict(self, seq: int) -> None:
        self.conflicts_seen += 1
        self.last_conflict_seq = seq
        if self.phase is ConflictPhase.RECOVERING:
            self._fail()
        elif self.phase is ConflictPhase.NONE:
            self.phase = ConflictPhase.BLOCKED

    def can_attempt(self) -> bool:
        return self.phase is ConflictPhase.BLOCKED and self.attempts < self.max_attempts

    def on_attempt(self, seq: int, mono_ns: int) -> bool:
        if not self.can_attempt():
            return False
        self.attempts += 1
        self.phase = ConflictPhase.RECOVERING
        self.attempt_started_seq = seq
        self.attempt_started_mono_ns = mono_ns
        return True

    def on_tick(self, mono_ns: int) -> None:
        if (self.phase is ConflictPhase.RECOVERING and self.attempt_started_mono_ns is not None
                and mono_ns - self.attempt_started_mono_ns >= self.attempt_timeout_ns):
            self._fail()

    def on_recovered(self) -> None:
        self.phase = ConflictPhase.NONE
        self.attempts = 0
        self.attempt_started_seq = None
        self.attempt_started_mono_ns = None
        self.recoveries += 1

    def reset_budget(self) -> None:
        """Meaningful connection/session change or explicit operator retry."""
        self.attempts = 0
        if self.phase in (ConflictPhase.EXHAUSTED, ConflictPhase.RECOVERING):
            self.phase = ConflictPhase.BLOCKED
        self.attempt_started_seq = None
        self.attempt_started_mono_ns = None

    def _fail(self) -> None:
        self.failures += 1
        self.attempt_started_seq = None
        self.attempt_started_mono_ns = None
        self.phase = ConflictPhase.EXHAUSTED if self.attempts >= self.max_attempts else ConflictPhase.BLOCKED
