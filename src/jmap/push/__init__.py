"""Push notifications: event source, WebSocket and PushSubscription.

Three transports for one idea. A ``StateChange`` says which types moved in which
accounts and nothing else, so whichever way it arrives - and whether or not some
are dropped - the follow-up is the same ``/changes`` call, and the client
converges. That is what makes push an optimisation rather than a second source of
truth.

Which transport to use is a property of where the client runs, not of the server:
an event source needs a held-open connection, a WebSocket needs one too but
carries API calls on it as well, and a PushSubscription needs neither because the
server dials out to a push service instead.
"""

from __future__ import annotations

from jmap.push.eventsource import (
    ALL_TYPES,
    CLOSE_AFTER_NO,
    CLOSE_AFTER_STATE,
    EventSourceError,
    EventStream,
    Ping,
    UnknownPushTypeError,
    event_source_url,
    parse_event,
)
from jmap.push.listener import AsyncEventSourceClient, EventSourceClient, PushListener
from jmap.push.sse import ServerSentEvent, SSEParser
from jmap.push.subscription import (
    InsecurePushUrlError,
    PendingVerification,
    application_server_key,
    check_push_url,
    mine,
    needs_recreating,
    new_subscription,
    renewal_update,
    verification_update,
)
from jmap.push.websocket import (
    SUBPROTOCOL,
    Incoming,
    RequestErrorMessage,
    ResponseMessage,
    SubprotocolError,
    WebSocketProtocol,
    WebSocketProtocolError,
    should_reauthenticate,
)
from jmap.push.websocket_aio import AsyncWebSocketClient
from jmap.push.websocket_client import WebSocketClient, WebSocketUnavailableError

__all__ = [
    "ALL_TYPES",
    "CLOSE_AFTER_NO",
    "CLOSE_AFTER_STATE",
    "SUBPROTOCOL",
    "AsyncEventSourceClient",
    "AsyncWebSocketClient",
    "EventSourceClient",
    "EventSourceError",
    "EventStream",
    "Incoming",
    "InsecurePushUrlError",
    "PendingVerification",
    "Ping",
    "PushListener",
    "RequestErrorMessage",
    "ResponseMessage",
    "SSEParser",
    "ServerSentEvent",
    "SubprotocolError",
    "UnknownPushTypeError",
    "WebSocketClient",
    "WebSocketProtocol",
    "WebSocketProtocolError",
    "WebSocketUnavailableError",
    "application_server_key",
    "check_push_url",
    "event_source_url",
    "mine",
    "needs_recreating",
    "new_subscription",
    "parse_event",
    "renewal_update",
    "should_reauthenticate",
    "verification_update",
]
