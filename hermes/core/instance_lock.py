"""One live Hermès per TWS API identity.

TWS accepts ONE API session per clientId. A second Hermès started with the same host/port/clientId
never receives ``nextValidId`` (TWS answers error 326 and closes the socket), so it retries forever
while the first one is healthy, and both processes share the same console, log files, recorder
directory, setup store and Slack app (Socket Mode spreads Slack button clicks across every open
connection, so a click can land in the process that knows nothing about the proposal).

``InstanceLock`` is an advisory, kernel-held ``flock`` on a small file. The kernel releases it when
the process exits for any reason (crash, SIGKILL, power loss of the process), so a stale file
never blocks a later start; the file content (pid, start time) is diagnostic only.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path


class InstanceLockError(RuntimeError):
    """Another live process already holds this TWS API identity."""

    def __init__(self, path: Path, holder: str) -> None:
        self.path = path
        self.holder = holder
        super().__init__(f"another Hermès instance already holds {path.name} ({holder or 'holder unknown'})")


def lock_name(host: str, port: int, client_id: int) -> str:
    safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", host) or "host"
    return f"tws-{safe_host}-{int(port)}-client{int(client_id)}.lock"


class InstanceLock:
    def __init__(self, directory: str | os.PathLike, host: str, port: int, client_id: int) -> None:
        self.path = Path(os.path.expanduser(str(directory))) / lock_name(host, port, client_id)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> InstanceLock:
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = os.pread(fd, 256, 0).decode("utf-8", "replace").strip()
            except OSError:
                holder = ""
            os.close(fd)
            raise InstanceLockError(self.path, holder) from None
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode(), 0)
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
