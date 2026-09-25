"""Stand-ins for an httpx-ws session, so the WebSocket clients run without a server.

A socket answers each frame it is sent through a ``respond`` callable, which
returns what the server says back: text frames, or exceptions to raise from the
receive that would read them.
"""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING, Any

from httpx_ws import WebSocketDisconnect

from jmap.capabilities.core import CORE_URN
from jmap.capabilities.mail import MAIL_URN
from jmap.capabilities.push import WEBSOCKET_URN
from jmap.testing import FakeJMAPServer

if TYPE_CHECKING:
    from collections.abc import Callable

    Respond = Callable[[dict[str, Any]], list[Any]]

WS_URL = "wss://jmap.example.com/jmap/ws"


def server(**websocket: Any) -> FakeJMAPServer:
    """A session offering mail and a WebSocket that carries push."""
    capability = {"url": WS_URL, "supportsPush": True, **websocket}
    return FakeJMAPServer(
        capabilities={CORE_URN: {}, MAIL_URN: {}, WEBSOCKET_URN: capability},
        primary_accounts={CORE_URN: "a", MAIL_URN: "a"},
    )


def answer(frame: dict[str, Any], **fields: Any) -> str:
    """A Response to ``frame``, answering each call as an empty ``/get``."""
    responses = [
        [name, {"accountId": "a", "state": "s1", "list": [], "notFound": []}, call_id]
        for name, _arguments, call_id in frame["methodCalls"]
    ]
    body = {"@type": "Response", "requestId": frame["id"], "methodResponses": responses}
    return json.dumps({**body, **fields})


def state_change(push_state: str = "p1") -> str:
    return json.dumps(
        {"@type": "StateChange", "changed": {"a": {"Email": "e2"}}, "pushState": push_state}
    )


def answer_requests(frame: dict[str, Any]) -> list[Any]:
    return [answer(frame)] if frame.get("@type") == "Request" else []


class SyncSocket:
    """A connected socket. Reading past what the server said is a normal close."""

    def __init__(self, respond: Respond = answer_requests, *, subprotocol: str | None = "jmap"):
        self.subprotocol = subprotocol
        self.respond = respond
        self.sent: list[dict[str, Any]] = []
        self.inbox: deque[Any] = deque()
        self.closed_with: tuple[int, str | None] | None = None
        self.send_error: BaseException | None = None

    def send_text(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        frame = json.loads(text)
        self.sent.append(frame)
        self.inbox.extend(self.respond(frame))

    def receive_text(self, timeout: float | None = None) -> str:
        if not self.inbox:
            raise WebSocketDisconnect(1000)
        item = self.inbox.popleft()
        if isinstance(item, BaseException):
            raise item
        return str(item)

    def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = (code, reason)
