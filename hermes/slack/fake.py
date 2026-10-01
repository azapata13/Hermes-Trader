"""Network-free Slack transport used by D1 unit/integration tests."""

from __future__ import annotations

import time
from threading import Lock

from hermes.slack.protocol import (
    InteractionHandler,
    SlackInteraction,
    SlackMessage,
    SlackMessageRef,
)


class FakeSlackTransport:
    def __init__(
        self,
        channel_id: str = "C_TEST",
        *,
        latency_s: float = 0.0,
        fail_posts: int = 0,
        fail_updates: int = 0,
    ) -> None:
        self.channel_id = channel_id
        self.latency_s = latency_s
        self.fail_posts = fail_posts
        self.fail_updates = fail_updates
        self.posts: list[tuple[SlackMessageRef, SlackMessage]] = []
        self.updates: list[tuple[SlackMessageRef, SlackMessage]] = []
        self.uploads: list[tuple[SlackMessageRef, str, str, str]] = []
        self._handler: InteractionHandler | None = None
        self._lock = Lock()
        self.started = False
        self.closed = False

    def start(self, on_interaction: InteractionHandler) -> None:
        self._handler = on_interaction
        self.started = True
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def post(self, message: SlackMessage) -> SlackMessageRef:
        if self.latency_s:
            time.sleep(self.latency_s)
        with self._lock:
            if self.fail_posts:
                self.fail_posts -= 1
                raise RuntimeError("injected Slack post failure")
            ref = SlackMessageRef(self.channel_id, f"{len(self.posts) + 1}.000001")
            self.posts.append((ref, message))
            return ref

    def update(self, ref: SlackMessageRef, message: SlackMessage) -> None:
        if self.latency_s:
            time.sleep(self.latency_s)
        with self._lock:
            if self.fail_updates:
                self.fail_updates -= 1
                raise RuntimeError("injected Slack update failure")
            self.updates.append((ref, message))

    def upload_file(
        self,
        ref: SlackMessageRef,
        path: str,
        *,
        title: str,
        initial_comment: str,
    ) -> None:
        with self._lock:
            self.uploads.append((ref, path, title, initial_comment))

    def emit(self, interaction: SlackInteraction) -> None:
        if self._handler is None:
            raise RuntimeError("fake Slack transport is not started")
        self._handler(interaction)
