"""Non-blocking logging: every logger writes to a queue; one listener thread does the I/O.

Files (under ``log_directory``, default ``~/hermes-data/logs``):
    hermes.log              human-readable application log (rotating)
    hermes-telemetry.jsonl  one JSON object per telemetry report (rotating)
Console: concise lines (stderr) when enabled.

ibapi's own loggers are raised to WARNING: they log every message at INFO/DEBUG.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
from pathlib import Path


class LoggingHandle:
    def __init__(self, listener: logging.handlers.QueueListener, log_dir: Path) -> None:
        self.listener = listener
        self.log_dir = log_dir

    def stop(self) -> None:
        self.listener.stop()


class _NotTelemetry(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("hermes.telemetry")


class _OnlyTelemetry(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("hermes.telemetry")


def setup_logging(log_directory: str, console: bool = True, level: int = logging.INFO) -> LoggingHandle:
    log_dir = Path(os.path.expanduser(log_directory))
    log_dir.mkdir(parents=True, exist_ok=True)

    app = logging.handlers.RotatingFileHandler(log_dir / "hermes.log", maxBytes=50 * 2**20, backupCount=10,
                                               encoding="utf-8")
    app.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-8s %(threadName)s %(name)s: %(message)s",
                                       "%Y-%m-%d %H:%M:%S"))
    app.addFilter(_NotTelemetry())

    tele = logging.handlers.RotatingFileHandler(log_dir / "hermes-telemetry.jsonl", maxBytes=50 * 2**20,
                                                backupCount=10, encoding="utf-8")
    tele.setFormatter(logging.Formatter("%(message)s"))
    tele.addFilter(_OnlyTelemetry())

    handlers: list[logging.Handler] = [app, tele]
    if console:
        con = logging.StreamHandler()
        con.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        con.addFilter(_NotTelemetry())
        handlers.append(con)

    q: queue.Queue = queue.Queue(-1)
    listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(logging.handlers.QueueHandler(q))
    root.setLevel(level)
    logging.getLogger("ibapi").setLevel(logging.WARNING)
    listener.start()
    return LoggingHandle(listener, log_dir)
