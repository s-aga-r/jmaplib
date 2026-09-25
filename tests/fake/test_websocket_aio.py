"""The async WebSocket client, over a stand-in socket (RFC 8887).

What the sync client cannot do is the point here: requests in flight together,
answered in any order, with push interleaved - so a reader task routes each
answer to its request by id.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import httpx_ws
import pytest
import wsproto.events
from httpx_ws import WebSocketInvalidTypeReceived, WebSocketNetworkError, WebSocketUpgradeError
from pydantic import ValidationError

from jmap.aio import AsyncJMAPClient
from jmap.auth import BasicAuth
from jmap.core.errors import AuthenticationError, RequestError, TransportError
from jmap.push.eventsource import UnknownPushTypeError
from jmap.push.websocket import CLOSE_INVALID_PAYLOAD, SubprotocolError, WebSocketProtocolError
from jmap.push.websocket_aio import AsyncWebSocketClient
from jmap.push.websocket_client import DEFAULT_MAX_MESSAGE_BYTES, WebSocketUnavailableError
from tests.fake.sockets import WS_URL, AsyncSocket, answer, server, state_change

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from jmap.testing import FakeJMAPServer

WELL_KNOWN = "https://jmap.example.com/.well-known/jmap"

#: Every wait below is bounded, so a routing bug fails rather than hangs.
DEADLINE = 5.0


async def aconnect(fake: FakeJMAPServer, *, timeout: float = 30.0) -> AsyncJMAPClient:
    return await AsyncJMAPClient.connect(
        WELL_KNOWN,
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.AsyncClient(**fake.client_kwargs(), timeout=timeout),
    )


@pytest.fixture
def dial(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[dict[str, Any]]]:
    """Answer the next handshake with ``socket``, or fail it with ``error``."""
    calls: list[dict[str, Any]] = []

    def install(socket: AsyncSocket | None = None, *, error: BaseException | None = None) -> Any:
        @contextlib.asynccontextmanager
        async def aconnect_ws(
            url: str, client: httpx.AsyncClient, **options: Any
        ) -> AsyncIterator[AsyncSocket]:
            calls.append({"url": url, "client": client, **options})
            if error is not None:
                raise error
            assert socket is not None
            yield socket
            calls.append({"closed": True})

        monkeypatch.setattr(httpx_ws, "aconnect_ws", aconnect_ws)
        return calls

    return install


async def mailbox_count(socket: AsyncWebSocketClient) -> int:
    async with socket.batch() as batch:
        mailboxes = batch.mail.mailbox.get(ids=None)
    return len(mailboxes.result.items)


class TestOpening:
    @pytest.mark.asyncio
    async def test_the_handshake_asks_for_the_jmap_subprotocol(self, dial):
        client = await aconnect(server())
        calls = dial(AsyncSocket())
        async with AsyncWebSocketClient(client) as socket:
            assert repr(socket).startswith("AsyncWebSocketClient(open")
        assert calls[0]["url"] == WS_URL
        assert calls[0]["client"] is client.http
        assert calls[0]["subprotocols"] == ["jmap"]
        assert calls[0]["max_message_size_bytes"] == DEFAULT_MAX_MESSAGE_BYTES
        assert calls[-1] == {"closed": True}
        assert repr(socket) == "AsyncWebSocketClient(closed, push_state=None)"

    @pytest.mark.asyncio
    async def test_a_refused_handshake_is_an_authentication_error(self, dial):
        dial(error=WebSocketUpgradeError(httpx.Response(401)))
        with pytest.raises(AuthenticationError):
            await AsyncWebSocketClient(await aconnect(server())).open()

    @pytest.mark.asyncio
    async def test_a_failed_dial_is_a_transport_error(self, dial):
        dial(error=WebSocketNetworkError())
        with pytest.raises(TransportError):
            await AsyncWebSocketClient(await aconnect(server())).open()

    @pytest.mark.asyncio
    async def test_a_server_speaking_another_subprotocol_is_refused(self, dial):
        calls = dial(AsyncSocket(subprotocol=None))
        with pytest.raises(SubprotocolError):
            await AsyncWebSocketClient(await aconnect(server())).open()
        assert calls[-1] == {"closed": True}

    @pytest.mark.asyncio
    async def test_using_it_before_opening_is_a_mistake(self):
        socket = AsyncWebSocketClient(await aconnect(server()))
        with pytest.raises(RuntimeError, match="not open"):
            await mailbox_count(socket)
        with pytest.raises(RuntimeError, match="not open"):
            await anext(socket.notifications())
        await socket.close()  # closing what never opened is harmless


class TestRequests:
    @pytest.mark.asyncio
    async def test_a_batch_travels_as_one_request_frame(self, dial):
        fake_socket = AsyncSocket()
        dial(fake_socket)
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            assert await mailbox_count(socket) == 0
        assert fake_socket.sent[0]["@type"] == "Request"

    @pytest.mark.asyncio
    async def test_requests_in_flight_together_are_answered_out_of_order(self, dial):
        held: list[dict[str, Any]] = []

        def respond(frame: dict[str, Any]) -> list[Any]:
            # Answer nothing until three requests wait, then the last one first.
            held.append(frame)
            if len(held) < 3:
                return []
            return [
                answer(each, methodResponses=[["Core/echo", {"n": index}, "c1"]])
                for index, each in reversed(list(enumerate(held)))
            ]

        dial(AsyncSocket(respond))
        results: dict[int, Any] = {}

        async def echo(socket: AsyncWebSocketClient, n: int) -> None:
            async with socket.batch() as batch:
                handle = batch.add("Core/echo", {"n": n})
            results[n] = handle.result

        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with anyio.fail_after(DEADLINE):
                async with anyio.create_task_group() as group:
                    for n in range(3):
                        group.start_soon(echo, socket, n)
        # Each request got the answer carrying its own id, whatever the order.
        request_of = {frame["id"]: frame["methodCalls"][0][1]["n"] for frame in held}
        assert results == {request_of[f"r{index + 1}"]: {"n": index} for index in range(3)}

    @pytest.mark.asyncio
    async def test_a_request_error_is_raised(self, dial):
        refusal = {"@type": "RequestError", "type": "urn:ietf:params:jmap:error:limit"}
        dial(AsyncSocket(lambda frame: [json.dumps({**refusal, "requestId": frame["id"]})]))
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with pytest.raises(RequestError):
                await mailbox_count(socket)

    @pytest.mark.asyncio
    async def test_an_answer_naming_no_request_goes_to_the_only_one_waiting(self, dial):
        dial(AsyncSocket(lambda frame: [answer(frame, requestId=None)]))
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with anyio.fail_after(DEADLINE):
                assert await mailbox_count(socket) == 0

    @pytest.mark.asyncio
    async def test_answers_that_fit_no_waiting_request_are_dropped(self, dial):
        held: list[dict[str, Any]] = []

        def respond(frame: dict[str, Any]) -> list[Any]:
            held.append(frame)
            if len(held) < 2:
                return []
            # With two waiting, an answer naming no request cannot be placed;
            # one naming a request nobody waits on belongs to no one.
            return [
                answer(frame, requestId=None),
                answer(frame, requestId="r99"),
                *(answer(each) for each in held),
            ]

        dial(AsyncSocket(respond))
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with anyio.fail_after(DEADLINE):
                async with anyio.create_task_group() as group:
                    group.start_soon(mailbox_count, socket)
                    group.start_soon(mailbox_count, socket)

    @pytest.mark.asyncio
    async def test_a_newer_session_state_marks_the_session_stale(self, dial):
        dial(AsyncSocket(lambda frame: [answer(frame, sessionState="newer")]))
        client = await aconnect(server())
        async with AsyncWebSocketClient(client) as socket:
            await mailbox_count(socket)
        assert client.session_stale

    @pytest.mark.asyncio
    async def test_an_unanswered_request_times_out(self, dial):
        dial(AsyncSocket(lambda frame: []))
        client = await aconnect(server(), timeout=0.05)
        async with AsyncWebSocketClient(client) as socket:
            with pytest.raises(TransportError, match="in time"):
                await mailbox_count(socket)

    @pytest.mark.parametrize(
        ("code", "raised", "message"),
        [
            (1000, TransportError, "closed before answering"),
            (1011, TransportError, r"\(1011\)"),
            (1008, AuthenticationError, "credentials"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_server_closing_mid_request(self, dial, code, raised, message):
        fake_socket = AsyncSocket(lambda frame: [])
        original = fake_socket.respond

        def close_instead(frame: dict[str, Any]) -> list[Any]:
            fake_socket.end(code)
            return original(frame)

        fake_socket.respond = close_instead
        dial(fake_socket)
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with anyio.fail_after(DEADLINE), pytest.raises(raised, match=message):
                await mailbox_count(socket)
            # And once it has closed, sending fails the same way.
            with pytest.raises(raised, match=message.replace("closed before answering", "closed")):
                await socket.disable_push()

    @pytest.mark.parametrize(
        ("failure", "raised", "message"),
        [
            (
                WebSocketInvalidTypeReceived(wsproto.events.BytesMessage(data=b"x")),
                WebSocketProtocolError,
                "text frames only",
            ),
            ("not json", WebSocketProtocolError, "I-JSON"),
            (WebSocketNetworkError(), TransportError, "WebSocketNetworkError"),
        ],
    )
    @pytest.mark.asyncio
    async def test_each_failure_while_reading_reaches_the_waiting_request(
        self, dial, failure, raised, message
    ):
        fake_socket = AsyncSocket(lambda frame: [failure])
        dial(fake_socket)
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with anyio.fail_after(DEADLINE), pytest.raises(raised, match=message):
                await mailbox_count(socket)
        if raised is WebSocketProtocolError:
            assert fake_socket.closed_with is not None
            assert fake_socket.closed_with[0] == CLOSE_INVALID_PAYLOAD

    @pytest.mark.asyncio
    async def test_a_failed_send_is_a_transport_error(self, dial):
        fake_socket = AsyncSocket()
        fake_socket.send_error = WebSocketNetworkError()
        dial(fake_socket)
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with pytest.raises(TransportError):
                await mailbox_count(socket)


class TestPush:
    @pytest.mark.asyncio
    async def test_enabling_push_names_the_types_and_resumes(self, dial):
        fake_socket = AsyncSocket()
        dial(fake_socket)
        async with AsyncWebSocketClient(await aconnect(server()), push_state="p0") as socket:
            await socket.enable_push(["Email"])
            await socket.disable_push()
        assert fake_socket.sent == [
            {"@type": "WebSocketPushEnable", "dataTypes": ["Email"], "pushState": "p0"},
            {"@type": "WebSocketPushDisable"},
        ]

    @pytest.mark.asyncio
    async def test_push_the_socket_cannot_deliver_is_refused(self, dial):
        dial(AsyncSocket())
        async with AsyncWebSocketClient(await aconnect(server(supportsPush=False))) as socket:
            with pytest.raises(WebSocketUnavailableError):
                await socket.enable_push()
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with pytest.raises(UnknownPushTypeError):
                await socket.enable_push(["Widget"])
            with pytest.raises(ValidationError):
                await socket.enable_push("Email")

    @pytest.mark.asyncio
    async def test_notifications_arrive_and_end_when_the_server_closes(self, dial):
        fake_socket = AsyncSocket()
        fake_socket.say(state_change("p1"), state_change("p2"))
        fake_socket.end()
        dial(fake_socket)
        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with anyio.fail_after(DEADLINE):
                changes = [change.push_state async for change in socket.notifications()]
        assert changes == ["p1", "p2"]
        assert socket.push_state == "p2"

    @pytest.mark.asyncio
    async def test_notifications_raise_when_the_socket_fails(self, dial):
        fake_socket = AsyncSocket()
        fake_socket.say(state_change("p1"))
        fake_socket.end(1011)
        dial(fake_socket)
        seen: list[str | None] = []

        async def collect(socket: AsyncWebSocketClient) -> None:
            async for change in socket.notifications():
                seen.append(change.push_state)

        async with AsyncWebSocketClient(await aconnect(server())) as socket:
            with anyio.fail_after(DEADLINE), pytest.raises(TransportError, match="1011"):
                await collect(socket)
        assert seen == ["p1"]
