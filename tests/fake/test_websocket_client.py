"""The sync WebSocket client, over a stand-in socket (RFC 8887).

The session comes from the fake server over HTTP; the socket is a script. What
is checked is the client's half: the handshake it asks for, the frames it
sends, how it matches answers and keeps push, and what each failure becomes.
"""

from __future__ import annotations

import contextlib
import json
import queue
from typing import TYPE_CHECKING, Any

import httpx
import httpx_ws
import pytest
import wsproto.events
from httpx_ws import (
    WebSocketDisconnect,
    WebSocketInvalidTypeReceived,
    WebSocketNetworkError,
    WebSocketUpgradeError,
)
from pydantic import ValidationError

from jmap.auth import BasicAuth
from jmap.client import JMAPClient
from jmap.core.errors import AuthenticationError, RequestError, TransportError
from jmap.core.session import InsecureEndpointError
from jmap.push.eventsource import UnknownPushTypeError
from jmap.push.websocket import CLOSE_INVALID_PAYLOAD, SubprotocolError, WebSocketProtocolError
from jmap.push.websocket_client import (
    DEFAULT_MAX_MESSAGE_BYTES,
    WebSocketClient,
    WebSocketUnavailableError,
)
from tests.fake.sockets import WS_URL, SyncSocket, answer, server, state_change

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from jmap.testing import FakeJMAPServer

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"


def connect(fake: FakeJMAPServer) -> JMAPClient:
    return JMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**fake.client_kwargs()),
    )


@pytest.fixture
def dial(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[dict[str, Any]]]:
    """Answer the next handshake with ``socket``, or fail it with ``error``."""
    calls: list[dict[str, Any]] = []

    def install(socket: SyncSocket | None = None, *, error: BaseException | None = None) -> Any:
        @contextlib.contextmanager
        def connect_ws(url: str, client: httpx.Client, **options: Any) -> Iterator[SyncSocket]:
            calls.append({"url": url, "client": client, **options})
            if error is not None:
                raise error
            assert socket is not None
            yield socket
            calls.append({"closed": True})

        monkeypatch.setattr(httpx_ws, "connect_ws", connect_ws)
        return calls

    return install


def mailbox_count(socket: WebSocketClient) -> int:
    with socket.batch() as batch:
        mailboxes = batch.mail.mailbox.get(ids=None)
    return len(mailboxes.result.items)


class TestFindingTheEndpoint:
    def test_a_server_with_no_websocket_is_refused_at_once(self):
        fake = server()
        fake.capabilities.pop("urn:ietf:params:jmap:websocket")
        with pytest.raises(WebSocketUnavailableError, match="does not advertise"):
            WebSocketClient(connect(fake))

    def test_a_cleartext_socket_from_a_tls_session_is_refused(self):
        # A downgrade: the credentials would leave a protected channel.
        with pytest.raises(InsecureEndpointError, match="WebSocket url"):
            WebSocketClient(connect(server(url="ws://jmap.example.com/jmap/ws")))

    def test_cleartext_on_loopback_is_allowed(self):
        assert WebSocketClient(connect(server(url="ws://localhost:8080/jmap/ws")))


class TestOpening:
    def test_the_handshake_asks_for_the_jmap_subprotocol(self, dial):
        client = connect(server())
        calls = dial(SyncSocket())
        with WebSocketClient(client):
            pass
        assert calls[0]["url"] == WS_URL
        assert calls[0]["client"] is client.http
        assert calls[0]["subprotocols"] == ["jmap"]
        assert calls[0]["max_message_size_bytes"] == DEFAULT_MAX_MESSAGE_BYTES
        assert calls[-1] == {"closed": True}

    def test_a_refused_handshake_is_an_authentication_error(self, dial):
        refusal = httpx.Response(401, headers={"www-authenticate": 'Bearer realm="jmap"'})
        dial(error=WebSocketUpgradeError(refusal))
        with pytest.raises(AuthenticationError) as excinfo:
            WebSocketClient(connect(server())).open()
        assert excinfo.value.challenges == ('Bearer realm="jmap"',)

    def test_any_other_refusal_is_a_request_error(self, dial):
        dial(error=WebSocketUpgradeError(httpx.Response(503)))
        with pytest.raises(RequestError) as excinfo:
            WebSocketClient(connect(server())).open()
        assert excinfo.value.status == 503

    def test_a_handshake_that_is_answered_but_not_upgraded(self, dial):
        dial(error=WebSocketUpgradeError(httpx.Response(200)))
        with pytest.raises(TransportError, match="HTTP 200, not 101"):
            WebSocketClient(connect(server())).open()

    def test_a_failed_dial_is_a_transport_error(self, dial):
        dial(error=WebSocketNetworkError())
        with pytest.raises(TransportError):
            WebSocketClient(connect(server())).open()

    def test_a_server_speaking_another_subprotocol_is_refused(self, dial):
        calls = dial(SyncSocket(subprotocol="chat"))
        with pytest.raises(SubprotocolError, match="'chat'"):
            WebSocketClient(connect(server())).open()
        assert calls[-1] == {"closed": True}

    def test_using_it_before_opening_is_a_mistake(self):
        socket = WebSocketClient(connect(server()))
        with pytest.raises(RuntimeError, match="not open"):
            mailbox_count(socket)
        socket.close()  # closing what never opened is harmless

    def test_repr_says_whether_it_is_open(self, dial):
        dial(SyncSocket())
        socket = WebSocketClient(connect(server()), push_state="p0")
        assert repr(socket) == "WebSocketClient(closed, push_state='p0')"
        with socket:
            assert repr(socket).startswith("WebSocketClient(open")


class TestRequests:
    def test_a_batch_travels_as_one_request_frame(self, dial):
        fake_socket = SyncSocket()
        dial(fake_socket)
        with WebSocketClient(connect(server())) as socket:
            assert mailbox_count(socket) == 0
        frame = fake_socket.sent[0]
        assert frame["@type"] == "Request"
        assert frame["id"] == "r1"
        assert frame["methodCalls"][0][0] == "Mailbox/get"

    def test_a_state_change_before_the_answer_is_kept_for_later(self, dial):
        dial(SyncSocket(lambda frame: [state_change("p7"), answer(frame)]))
        with WebSocketClient(connect(server())) as socket:
            mailbox_count(socket)
            changes = list(socket.notifications())
        assert [change.push_state for change in changes] == ["p7"]
        assert socket.push_state == "p7"

    def test_a_request_error_is_raised(self, dial):
        refusal = {"@type": "RequestError", "type": "urn:ietf:params:jmap:error:limit"}
        dial(SyncSocket(lambda frame: [json.dumps({**refusal, "requestId": frame["id"]})]))
        with WebSocketClient(connect(server())) as socket, pytest.raises(RequestError):
            mailbox_count(socket)

    def test_an_answer_naming_no_request_is_the_one_in_flight(self, dial):
        dial(SyncSocket(lambda frame: [answer(frame, requestId=None)]))
        with WebSocketClient(connect(server())) as socket:
            assert mailbox_count(socket) == 0

    def test_an_answer_to_another_request_is_refused(self, dial):
        fake_socket = SyncSocket(lambda frame: [answer(frame, requestId="r99")])
        dial(fake_socket)
        with (
            WebSocketClient(connect(server())) as socket,
            pytest.raises(WebSocketProtocolError, match="r99"),
        ):
            mailbox_count(socket)
        assert fake_socket.closed_with is not None
        assert fake_socket.closed_with[0] == CLOSE_INVALID_PAYLOAD

    def test_a_newer_session_state_marks_the_session_stale(self, dial):
        dial(SyncSocket(lambda frame: [answer(frame, sessionState="newer")]))
        client = connect(server())
        with WebSocketClient(client) as socket:
            mailbox_count(socket)
        assert client.session_stale

    @pytest.mark.parametrize(
        ("failure", "raised", "message"),
        [
            (queue.Empty(), TransportError, "no answer"),
            (WebSocketDisconnect(1000), TransportError, r"\(1000\)"),
            (WebSocketDisconnect(1011), TransportError, r"\(1011\)"),
            (WebSocketDisconnect(1008, "token expired"), AuthenticationError, "1008: token"),
            (WebSocketNetworkError(), TransportError, "WebSocketNetworkError"),
            (
                WebSocketInvalidTypeReceived(wsproto.events.BytesMessage(data=b"x")),
                WebSocketProtocolError,
                "text frames only",
            ),
            ("not json", WebSocketProtocolError, "I-JSON"),
        ],
    )
    def test_each_failure_while_waiting_becomes_a_library_error(
        self, dial, failure, raised, message
    ):
        dial(SyncSocket(lambda frame: [failure]))
        with WebSocketClient(connect(server())) as socket, pytest.raises(raised, match=message):
            mailbox_count(socket)

    def test_a_failed_send_is_a_transport_error(self, dial):
        fake_socket = SyncSocket()
        fake_socket.send_error = WebSocketNetworkError()
        dial(fake_socket)
        with WebSocketClient(connect(server())) as socket, pytest.raises(TransportError):
            mailbox_count(socket)


class TestPush:
    def test_enabling_push_names_the_types_and_resumes(self, dial):
        fake_socket = SyncSocket()
        dial(fake_socket)
        with WebSocketClient(connect(server()), push_state="p0") as socket:
            socket.enable_push(["Email"])
            socket.disable_push()
        assert fake_socket.sent == [
            {"@type": "WebSocketPushEnable", "dataTypes": ["Email"], "pushState": "p0"},
            {"@type": "WebSocketPushDisable"},
        ]

    def test_enabling_push_for_every_type(self, dial):
        fake_socket = SyncSocket()
        dial(fake_socket)
        with WebSocketClient(connect(server())) as socket:
            socket.enable_push()
        assert fake_socket.sent == [{"@type": "WebSocketPushEnable", "dataTypes": None}]

    def test_a_socket_without_push_says_so(self, dial):
        dial(SyncSocket())
        with (
            WebSocketClient(connect(server(supportsPush=False))) as socket,
            pytest.raises(WebSocketUnavailableError, match="does not carry push"),
        ):
            socket.enable_push()

    def test_a_type_the_server_cannot_push_is_refused(self, dial):
        dial(SyncSocket())
        with (
            WebSocketClient(connect(server())) as socket,
            pytest.raises(UnknownPushTypeError, match="Widget"),
        ):
            socket.enable_push(["Widget"])

    def test_one_type_name_is_not_a_list_of_them(self, dial):
        dial(SyncSocket())
        with (
            WebSocketClient(connect(server())) as socket,
            pytest.raises(ValidationError, match="enable_push"),
        ):
            socket.enable_push("Email")

    def test_notifications_end_when_the_server_closes_normally(self, dial):
        fake_socket = SyncSocket()
        fake_socket.inbox.extend([state_change("p1"), state_change("p2")])
        dial(fake_socket)
        with WebSocketClient(connect(server())) as socket:
            assert [change.push_state for change in socket.notifications()] == ["p1", "p2"]

    def test_an_answer_with_nothing_in_flight_is_refused(self, dial):
        fake_socket = SyncSocket()
        fake_socket.inbox.append(answer({"id": "r1", "methodCalls": []}))
        dial(fake_socket)
        with (
            WebSocketClient(connect(server())) as socket,
            pytest.raises(WebSocketProtocolError, match="no request in flight"),
        ):
            list(socket.notifications())
