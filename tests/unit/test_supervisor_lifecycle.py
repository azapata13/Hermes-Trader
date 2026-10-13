"""TWS connection lifecycle (live investigation: 'connecting to TWS ... (attempt 143)' while healthy).

The Supervisor is the ONLY caller of connect(): one loop, one socket at a time, no timers. These tests
pin that down with a scripted client (no network) so a regression is caught deterministically:

* a healthy connection never triggers a second connect attempt;
* a lost connection reconnects (once per loss, with backoff), and the backoff resets after success;
* the reconnect wait is cancelled by stop: nothing fires afterwards;
* a second concurrent run() is refused (one connection owner);
* TWS error 326 / a missing nextValidId are detected and retried with bounded exponential backoff.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from types import SimpleNamespace
from typing import ClassVar

import pytest

from hermes.config import load_config
from hermes.ibkr import session as session_mod
from hermes.ibkr.session import Supervisor

INITIAL, MAXB = 0.02, 0.16


class ScriptedClient:
    """Stand-in for ReadOnlyClient. Each connect() consumes one mode from ``script``:
    ok            handshake (nextValidId) and stay up until disconnect()
    ok:<s>        handshake, then the socket drops after <s> seconds (connection lost)
    reject        TWS error 326, then TWS closes the socket
    silent        socket up, never any nextValidId
    closed        socket closed by TWS before nextValidId
    refused       socket connect fails"""

    script: ClassVar[list[str]] = []
    session = None
    log: ClassVar[list[tuple[str, float]]] = []
    live = 0
    max_live = 0
    lock = threading.Lock()

    def __init__(self, adapter) -> None:
        self._stop = threading.Event()
        self.mode = "refused"
        self.readonly_violations = []

    def connect(self, host, port, client_id) -> None:
        cls = ScriptedClient
        self.mode = cls.script.pop(0) if cls.script else "silent"
        cls.log.append(("connect", time.monotonic()))
        if self.mode != "refused":
            with cls.lock:
                cls.live += 1
                cls.max_live = max(cls.max_live, cls.live)

    def isConnected(self) -> bool:  # noqa: N802
        return self.mode != "refused" and not self._stop.is_set()

    def run(self) -> None:
        s = ScriptedClient.session
        try:
            if self.mode.startswith("ok"):
                s.connected.set()
                life = float(self.mode.split(":")[1]) if ":" in self.mode else None
                self._stop.wait(life)
            elif self.mode == "reject":
                s.client_id_rejected.set()
            elif self.mode == "silent":
                self._stop.wait()
        finally:
            s.connected.clear()
            with ScriptedClient.lock:
                ScriptedClient.live -= 1

    def disconnect(self) -> None:
        ScriptedClient.log.append(("disconnect", time.monotonic()))
        self._stop.set()


def _cfg(connect_timeout=0.3):
    c = load_config()
    return dataclasses.replace(
        c, ibkr=dataclasses.replace(c.ibkr, connect_timeout_s=connect_timeout),
        session=dataclasses.replace(c.session, reconnect_initial_backoff_s=INITIAL, reconnect_max_backoff_s=MAXB,
                                    shutdown_grace_s=0.0))


@pytest.fixture
def sup(monkeypatch):
    def make(script, connect_timeout=0.3):
        ScriptedClient.script = list(script)
        ScriptedClient.log = []
        ScriptedClient.live = ScriptedClient.max_live = 0
        sess = SimpleNamespace(connected=threading.Event(), client_id_rejected=threading.Event(),
                               shutdown_requests=lambda: None)
        ScriptedClient.session = sess
        monkeypatch.setattr(session_mod, "ReadOnlyClient", ScriptedClient)
        pipeline = SimpleNamespace(post_local=lambda *a, **k: None, pump=lambda: None,
                                   publisher=SimpleNamespace(latest=lambda: None), on_callback=lambda *a: None)
        gateway = SimpleNamespace(bind=lambda c: None, unbind=lambda: None)
        s = Supervisor(_cfg(connect_timeout), pipeline, gateway, sess)
        sleeps: list[float] = []
        real_sleep = s._sleep

        def recording_sleep(seconds, deadline):
            sleeps.append(round(seconds, 4))
            real_sleep(seconds, deadline)

        s._sleep = recording_sleep
        return s, sleeps
    return make


def _connects():
    return sum(1 for k, _ in ScriptedClient.log if k == "connect")


def _bg(s, duration=None):
    t = threading.Thread(target=s.run, args=(duration,), daemon=True)
    t.start()
    return t


def test_healthy_connection_never_triggers_a_second_connect_attempt(sup):
    s, sleeps = sup(["ok"])
    t = _bg(s)
    time.sleep(1.0)                                # many reconnect periods would have elapsed
    s.request_stop("test")
    t.join(5)
    assert not t.is_alive()
    assert _connects() == 1 and s.connect_attempts == 1
    assert ScriptedClient.max_live == 1 and sleeps == []
    assert s.last_handshake == "ready"


def test_lost_connection_reconnects_once_and_backoff_resets_after_success(sup):
    s, sleeps = sup(["closed", "closed", "ok:0.1", "closed", "ok"])
    t = _bg(s)
    deadline = time.monotonic() + 5
    while _connects() < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.3)
    s.request_stop("test")
    t.join(5)
    assert _connects() == 5                        # exactly one attempt per loss, none while healthy
    assert ScriptedClient.max_live == 1            # never two sockets at once
    # 2 failures -> 0.02, 0.04 ; success then loss -> back to the initial delay ; failure -> doubled
    assert sleeps == [INITIAL, 2 * INITIAL, INITIAL, 2 * INITIAL]


def test_stop_cancels_the_pending_reconnect_wait_and_nothing_fires_afterwards(sup, monkeypatch):
    s, _ = sup(["closed"])
    s.cfg = dataclasses.replace(s.cfg, session=dataclasses.replace(s.cfg.session, reconnect_initial_backoff_s=30.0,
                                                                 reconnect_max_backoff_s=60.0))
    t = _bg(s)
    while _connects() < 1:
        time.sleep(0.01)
    time.sleep(0.1)                                 # now inside the 30 s reconnect wait
    t0 = time.monotonic()
    s.request_stop("test")
    t.join(5)
    assert not t.is_alive() and time.monotonic() - t0 < 1.0
    time.sleep(0.2)
    assert _connects() == 1


def test_a_second_concurrent_run_is_refused(sup):
    s, _ = sup(["ok"])
    t = _bg(s)
    while _connects() < 1:
        time.sleep(0.01)
    with pytest.raises(RuntimeError, match="already active"):
        s.run(0.2)
    assert _connects() == 1 and ScriptedClient.max_live == 1
    s.request_stop("test")
    t.join(5)


def test_client_id_in_use_is_detected_at_once_and_retried_with_bounded_backoff(sup, caplog):
    s, sleeps = sup(["reject"] * 6, connect_timeout=5.0)
    caplog.set_level(logging.WARNING, logger="hermes.session")
    t0 = time.monotonic()
    t = _bg(s)
    deadline = time.monotonic() + 5
    while _connects() < 6 and time.monotonic() < deadline:
        time.sleep(0.01)
    s.request_stop("test")
    t.join(5)
    assert _connects() == 6
    # no 5 s nextValidId wait per attempt: the 326 ends the attempt immediately
    assert time.monotonic() - t0 < 2.0
    assert sleeps[:5] == [INITIAL, 2 * INITIAL, 4 * INITIAL, MAXB, MAXB]   # exponential, capped
    assert s.client_id_rejections >= 5 and s.last_handshake == "rejected"
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records if "error 326" in r.getMessage()]
    assert msgs[0][0] == logging.WARNING and all(lvl == logging.CRITICAL for lvl, _ in msgs[1:])
    assert "ps -axo pid,lstart,command" in msgs[-1][1]


def test_missing_next_valid_id_times_out_and_backs_off_without_a_storm(sup, caplog):
    s, sleeps = sup(["silent"] * 4, connect_timeout=0.1)
    caplog.set_level(logging.ERROR, logger="hermes.session")
    t = _bg(s)
    deadline = time.monotonic() + 5
    while _connects() < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    s.request_stop("test")
    t.join(5)
    assert sleeps[:3] == [INITIAL, 2 * INITIAL, 4 * INITIAL]
    assert ScriptedClient.max_live == 1            # each silent socket is closed before the next try
    disconnects = sum(1 for k, _ in ScriptedClient.log if k == "disconnect")
    assert disconnects >= 3
    assert sum("no nextValidId" in r.getMessage() for r in caplog.records) >= 3


def test_socket_closed_before_handshake_is_reported_as_such(sup, caplog):
    s, _ = sup(["closed"], connect_timeout=5.0)
    caplog.set_level(logging.ERROR, logger="hermes.session")
    t0 = time.monotonic()
    t = _bg(s)
    while _connects() < 1:
        time.sleep(0.01)
    time.sleep(0.2)
    s.request_stop("test")
    t.join(5)
    assert time.monotonic() - t0 < 2.0
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "closed the connection before nextValidId" in text and "no nextValidId within" not in text
