"""One live Hermès per TWS API identity (host, port, clientId)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from hermes.core.instance_lock import InstanceLock, InstanceLockError, lock_name


def test_second_holder_is_refused_and_names_the_first(tmp_path):
    a = InstanceLock(tmp_path, "127.0.0.1", 7496, 110).acquire()
    try:
        with pytest.raises(InstanceLockError) as exc:
            InstanceLock(tmp_path, "127.0.0.1", 7496, 110).acquire()
        assert f"pid={os.getpid()}" in exc.value.holder
        assert exc.value.path.name == lock_name("127.0.0.1", 7496, 110)
    finally:
        a.release()
    InstanceLock(tmp_path, "127.0.0.1", 7496, 110).acquire().release()     # free again


def test_distinct_identities_do_not_conflict(tmp_path):
    with InstanceLock(tmp_path, "127.0.0.1", 7496, 110), InstanceLock(tmp_path, "127.0.0.1", 7496, 111), \
            InstanceLock(tmp_path, "127.0.0.1", 7497, 110):
        pass


def test_a_leftover_file_without_a_live_holder_never_blocks(tmp_path):
    path = tmp_path / lock_name("127.0.0.1", 7496, 110)
    path.write_text("pid=999999 started=yesterday\n")
    with InstanceLock(tmp_path, "127.0.0.1", 7496, 110) as lk:
        assert lk.held and f"pid={os.getpid()}" in path.read_text()


def test_lock_held_by_another_process_is_released_when_that_process_dies(tmp_path):
    code = ("import sys, time; sys.path.insert(0, %r); from hermes.core.instance_lock import InstanceLock; "
            "InstanceLock(%r, '127.0.0.1', 7496, 110).acquire(); print('held', flush=True); time.sleep(60)"
            % (os.getcwd(), str(tmp_path)))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        with pytest.raises(InstanceLockError) as exc:
            InstanceLock(tmp_path, "127.0.0.1", 7496, 110).acquire()
        assert f"pid={p.pid}" in exc.value.holder
        p.send_signal(signal.SIGKILL)                      # a crash, not a clean exit
        p.wait(5)
        deadline = time.monotonic() + 3
        while True:
            try:
                InstanceLock(tmp_path, "127.0.0.1", 7496, 110).acquire().release()
                break
            except InstanceLockError:
                assert time.monotonic() < deadline, "kernel did not release the dead holder's lock"
                time.sleep(0.05)
    finally:
        if p.poll() is None:
            p.kill()
