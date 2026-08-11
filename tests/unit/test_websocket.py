"""The RFC 8887 WebSocket protocol layer.

I/O-free, so the rules that matter are testable directly: out-of-order responses
correlated by id, request-level errors delivered as messages rather than status
codes, and ``pushState`` as the cheap way back after a drop.
"""

from __future__ import annotations

from typing import Any

import pytest

from jmap.core.errors import RequestError
from jmap.core.ijson import loads
from jmap.core.invocation import MethodCall
from jmap.core.request import Request
from jmap.models.push import StateChange
from jmap.push.websocket import (
    CLOSE_INVALID_PAYLOAD,
    CLOSE_POLICY_VIOLATION,
    CLOSE_UNSUPPORTED_DATA,
    SUBPROTOCOL,
    RequestErrorMessage,
    ResponseMessage,
    SubprotocolError,
    WebSocketProtocol,
    WebSocketProtocolError,
    should_reauthenticate,
)


def decoded(text: str) -> Any:
    return loads(text.encode())


def request() -> Request:
    return Request(
        using=frozenset({"urn:ietf:params:jmap:core"}),
        method_calls=[("c1", MethodCall("Core/echo", {"hello": True}, parse=dict))],
    )


class TestEncoding:
    def test_a_request_is_tagged_and_identified(self):
        protocol = WebSocketProtocol()
        body = decoded(protocol.encode_request(request()))
        assert body["@type"] == "Request"
        assert body["id"] == "r1"
        assert body["methodCalls"][0][0] == "Core/echo"

    def test_ids_are_unique_per_connection(self):
        protocol = WebSocketProtocol()
        first = decoded(protocol.encode_request(request()))["id"]
        second = decoded(protocol.encode_request(request()))["id"]
        assert first != second

    def test_a_caller_supplied_id_is_used_verbatim(self):
        protocol = WebSocketProtocol()
        assert decoded(protocol.encode_request(request(), "mine"))["id"] == "mine"

    def test_sent_requests_are_tracked_until_answered(self):
        protocol = WebSocketProtocol()
        protocol.encode_request(request(), "r9")
        assert protocol.outstanding == {"r9"}
        protocol.settle("r9")
        assert protocol.outstanding == set()

    def test_settling_an_unknown_id_is_harmless(self):
        # A server may answer with a requestId we never sent; dropping it beats
        # raising on a message we could simply ignore.
        protocol = WebSocketProtocol()
        protocol.settle("never-sent")
        protocol.settle(None)
        assert protocol.outstanding == set()

    def test_push_enable_carries_the_requested_types(self):
        body = decoded(WebSocketProtocol().encode_push_enable(["Email", "Mailbox"]))
        assert body["@type"] == "WebSocketPushEnable"
        assert body["dataTypes"] == ["Email", "Mailbox"]

    def test_push_enable_defaults_to_every_type(self):
        assert decoded(WebSocketProtocol().encode_push_enable())["dataTypes"] is None

    def test_push_disable_is_a_bare_tag(self):
        assert decoded(WebSocketProtocol().encode_push_disable()) == {
            "@type": "WebSocketPushDisable"
        }


class TestPushState:
    def test_the_token_is_captured_from_a_notification(self):
        protocol = WebSocketProtocol()
        protocol.decode('{"@type":"StateChange","changed":{},"pushState":"bbb"}')
        assert protocol.push_state == "bbb"

    def test_a_notification_without_one_leaves_it_alone(self):
        # Not every server supports pushState; clearing it on a notification that
        # omits one would throw away a usable resume token.
        protocol = WebSocketProtocol()
        protocol.decode('{"@type":"StateChange","changed":{},"pushState":"bbb"}')
        protocol.decode('{"@type":"StateChange","changed":{}}')
        assert protocol.push_state == "bbb"

    def test_reconnecting_sends_the_token_back(self):
        # §4.3.5.2: the server should then send everything missed, which is one
        # exchange instead of a /changes call per type.
        protocol = WebSocketProtocol()
        protocol.decode('{"@type":"StateChange","changed":{},"pushState":"ccc"}')
        assert decoded(protocol.encode_push_enable(["Email"]))["pushState"] == "ccc"

    def test_resume_can_be_declined(self):
        protocol = WebSocketProtocol()
        protocol.decode('{"@type":"StateChange","changed":{},"pushState":"ccc"}')
        assert "pushState" not in decoded(protocol.encode_push_enable(resume=False))

    def test_nothing_is_sent_when_there_is_no_token(self):
        assert "pushState" not in decoded(WebSocketProtocol().encode_push_enable())


class TestDecoding:
    def test_a_response_is_correlated_by_request_id(self):
        # §4.3.2 lets the server answer out of order, so this is the only thing
        # pairing a response with its request.
        protocol = WebSocketProtocol()
        message = protocol.decode(
            '{"@type":"Response","requestId":"r1","methodResponses":'
            '[["Core/echo",{"hello":true},"c1"]],"sessionState":"s0"}'
        )
        assert isinstance(message, ResponseMessage)
        assert message.request_id == "r1"
        assert message.response.method_responses[0].name == "Core/echo"

    def test_a_response_without_an_id_still_parses(self):
        # Legal when the request carried none - the client just cannot correlate.
        protocol = WebSocketProtocol()
        message = protocol.decode('{"@type":"Response","methodResponses":[],"sessionState":"s"}')
        assert isinstance(message, ResponseMessage)
        assert message.request_id is None

    def test_a_non_string_request_id_is_treated_as_absent(self):
        protocol = WebSocketProtocol()
        message = protocol.decode(
            '{"@type":"Response","requestId":7,"methodResponses":[],"sessionState":"s"}'
        )
        assert isinstance(message, ResponseMessage)
        assert message.request_id is None

    def test_a_state_change_is_recognised(self):
        protocol = WebSocketProtocol()
        message = protocol.decode(
            '{"@type":"StateChange","changed":{"a123":{"Email":"0af7a512ce70"}}}'
        )
        assert isinstance(message, StateChange)
        assert message.states_for("a123") == {"Email": "0af7a512ce70"}

    def test_a_request_error_is_a_message_not_a_status(self):
        # There is no HTTP status on a socket, so the RFC 7807 body arrives as a
        # frame like everything else - and names which request it killed.
        protocol = WebSocketProtocol()
        message = protocol.decode(
            '{"@type":"RequestError","requestId":"r1",'
            '"type":"urn:ietf:params:jmap:error:notJSON","status":400}'
        )
        assert isinstance(message, RequestErrorMessage)
        assert message.request_id == "r1"
        assert isinstance(message.error, RequestError)
        assert message.error.status == 400

    def test_a_request_error_may_name_no_request(self):
        # The RFC's own example: the server could not parse far enough to know.
        protocol = WebSocketProtocol()
        message = protocol.decode(
            '{"@type":"RequestError","requestId":null,'
            '"type":"urn:ietf:params:jmap:error:notJSON","status":400}'
        )
        assert isinstance(message, RequestErrorMessage)
        assert message.request_id is None

    def test_an_unknown_tag_is_refused(self):
        # §4.3.1 lets the client answer this with a 1007 close.
        with pytest.raises(WebSocketProtocolError, match="unexpected @type"):
            WebSocketProtocol().decode('{"@type":"Nonsense"}')

    def test_an_untagged_message_is_refused(self):
        with pytest.raises(WebSocketProtocolError, match="unexpected @type"):
            WebSocketProtocol().decode('{"methodResponses":[]}')

    def test_a_json_scalar_is_refused(self):
        with pytest.raises(WebSocketProtocolError, match="JSON object"):
            WebSocketProtocol().decode("42")


class TestConnectionRules:
    def test_the_subprotocol_name_is_the_one_the_rfc_registers(self):
        assert SUBPROTOCOL == "jmap"

    def test_a_handshake_without_agreement_names_what_came_back(self):
        # Proceeding would misparse the first frame rather than reject it.
        error = SubprotocolError("chat")
        assert error.negotiated == "chat"
        assert "jmap" in str(error)

    def test_a_policy_violation_close_means_reauthenticate(self):
        # §4.1: the credentials that authenticated the handshake expired.
        # Redialling with the same ones produces the same close, forever.
        assert should_reauthenticate(CLOSE_POLICY_VIOLATION) is True

    def test_other_closes_are_retryable(self):
        assert should_reauthenticate(CLOSE_INVALID_PAYLOAD) is False
        assert should_reauthenticate(CLOSE_UNSUPPORTED_DATA) is False
        assert should_reauthenticate(None) is False

    def test_the_close_codes_are_the_rfc_6455_ones(self):
        assert (CLOSE_UNSUPPORTED_DATA, CLOSE_INVALID_PAYLOAD, CLOSE_POLICY_VIOLATION) == (
            1003,
            1007,
            1008,
        )

    def test_the_repr_summarises_the_connection(self):
        protocol = WebSocketProtocol()
        protocol.encode_request(request(), "r1")
        assert "outstanding=1" in repr(protocol)
