"""Push models, event-source URL building, subscriptions and VAPID."""

from __future__ import annotations

import httpx
import pytest

from jmap.capabilities.push import (
    VAPID,
    VAPID_URN,
    WEBSOCKET,
    WEBSOCKET_URN,
    VapidCapability,
    WebSocketCapability,
    vapid_key_rotated,
)
from jmap.core.session import Session
from jmap.models.push import (
    TYPE_PUSH_DISABLE,
    TYPE_PUSH_ENABLE,
    TYPE_PUSH_VERIFICATION,
    TYPE_STATE_CHANGE,
    PushKeys,
    PushSubscription,
    PushVerification,
    StateChange,
    WebSocketPushDisable,
    WebSocketPushEnable,
)
from jmap.push.eventsource import (
    CLOSE_AFTER_NO,
    CLOSE_AFTER_STATE,
    MAX_PORTABLE_PING,
    MIN_PORTABLE_PING,
    EventSourceError,
    EventStream,
    Ping,
    UnknownPushTypeError,
    event_source_url,
    parse_event,
    resume_headers,
    stream_headers,
)
from jmap.push.listener import PING_TIMEOUT_SLACK, PushListener, stream_timeout
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

EVENT_SOURCE = "https://jmap.example.com/es/?types={types}&closeafter={closeafter}&ping={ping}"


def session(**capabilities: object) -> Session:
    return Session.from_wire(
        {
            "capabilities": {"urn:ietf:params:jmap:core": {}, **capabilities},
            "accounts": {},
            "primaryAccounts": {},
            "username": "alice",
            "apiUrl": "https://jmap.example.com/jmap/",
            "eventSourceUrl": EVENT_SOURCE,
            "state": "s0",
        }
    )


class TestStateChange:
    def test_the_rfc_example_parses(self):
        change = StateChange.model_validate(
            {
                "@type": "StateChange",
                "changed": {
                    "a3123": {"Email": "d35ecb040aab", "EmailDelivery": "428d565f2440"},
                    "a43461d": {"Mailbox": "0af7a512ce70"},
                },
            }
        )
        assert change.states_for("a3123")["Email"] == "d35ecb040aab"
        assert sorted(change.accounts()) == ["a3123", "a43461d"]
        assert change.types() == {"Email", "EmailDelivery", "Mailbox"}

    def test_an_account_that_did_not_change_reads_as_empty(self):
        assert StateChange().states_for("nobody") == {}

    def test_outdated_reports_only_what_moved(self):
        change = StateChange.model_validate({"changed": {"a": {"Email": "e2", "Mailbox": "m1"}}})
        stale = change.outdated({"a": {"Email": "e1", "Mailbox": "m1"}})
        assert stale == {"a": {"Email": "e2"}}

    def test_a_type_never_synced_counts_as_outdated(self):
        # Never having fetched a type is the strongest reason to fetch it.
        change = StateChange.model_validate({"changed": {"a": {"Email": "e1"}}})
        assert change.outdated({}) == {"a": {"Email": "e1"}}

    def test_nothing_outdated_when_everything_matches(self):
        # The common case for a client that just made the change itself.
        change = StateChange.model_validate({"changed": {"a": {"Email": "e1"}}})
        assert change.outdated({"a": {"Email": "e1"}}) == {}

    def test_matches_recognises_our_own_write(self):
        # RFC 8620 §7.1: a notification can arrive while your own /set is in
        # flight, and re-fetching what you just wrote is a wasted round trip.
        change = StateChange.model_validate({"changed": {"a": {"Email": "e2"}}})
        assert change.matches("a", "Email", "e2")
        assert not change.matches("a", "Email", "e1")
        assert not change.matches("b", "Email", "e2")

    def test_push_state_is_carried_when_present(self):
        change = StateChange.model_validate({"changed": {}, "pushState": "bbb"})
        assert change.push_state == "bbb"

    def test_push_state_is_absent_without_websocket_support(self):
        assert StateChange().push_state is None


class TestTheTypeTag:
    def test_the_tag_survives_serialisation(self):
        # exclude_unset would drop a tag nobody assigned - and over a WebSocket
        # the tag is the only thing that says what the message is.
        assert WebSocketPushDisable().to_wire() == {"@type": TYPE_PUSH_DISABLE}

    def test_each_class_carries_its_own_tag(self):
        assert StateChange().to_wire()["@type"] == TYPE_STATE_CHANGE
        assert PushVerification().to_wire()["@type"] == TYPE_PUSH_VERIFICATION
        assert WebSocketPushEnable().to_wire()["@type"] == TYPE_PUSH_ENABLE

    def test_a_tag_from_the_wire_is_preserved(self):
        parsed = StateChange.model_validate({"@type": "StateChange", "changed": {}})
        assert parsed.to_wire()["@type"] == TYPE_STATE_CHANGE

    def test_push_enable_sends_only_what_was_set(self):
        enable = WebSocketPushEnable(dataTypes=["Email"])
        assert enable.to_wire() == {"@type": TYPE_PUSH_ENABLE, "dataTypes": ["Email"]}

    def test_null_data_types_means_everything_and_is_kept(self):
        # Distinct from omitting it, though both happen to mean "all" here - the
        # explicit null is what a caller writes to say so deliberately.
        assert WebSocketPushEnable(dataTypes=None).to_wire()["dataTypes"] is None


class TestPushSubscriptionModel:
    def test_the_rfc_example_parses(self):
        subscription = PushSubscription.from_wire(
            {
                "id": "e50b2c1d",
                "deviceClientId": "b37ff8001ca0",
                "verificationCode": "b210ef734fe5f439c1ca386421359f7b",
                "expires": "2018-07-31T00:13:21Z",
                "types": ["Todo"],
            }
        )
        assert subscription.device_client_id == "b37ff8001ca0"
        assert subscription.types == ["Todo"]

    def test_url_and_keys_are_absent_from_a_fetched_subscription(self):
        # The server never returns them, so a model that defaulted them to
        # anything but None would be lying about what it holds.
        subscription = PushSubscription.from_wire({"id": "p1"})
        assert subscription.url is None
        assert subscription.keys is None

    def test_keys_parse_when_given(self):
        subscription = PushSubscription.from_wire(
            {"id": "p1", "keys": {"p256dh": "abc", "auth": "def"}}
        )
        assert subscription.keys == PushKeys(p256dh="abc", auth="def")

    def test_null_types_means_every_type(self):
        assert PushSubscription.from_wire({"types": None}).types is None


class TestEventSourceUrl:
    def test_no_types_becomes_the_wildcard(self):
        assert "types=%2A" in event_source_url(session()) or "types=*" in event_source_url(
            session()
        )

    def test_types_are_comma_separated(self):
        url = event_source_url(session(), types=("Email", "Mailbox"))
        assert "Email%2CMailbox" in url or "Email,Mailbox" in url

    def test_close_after_and_ping_are_substituted(self):
        url = event_source_url(session(), close_after=CLOSE_AFTER_STATE, ping=300)
        assert "closeafter=state" in url
        assert "ping=300" in url

    def test_the_default_is_a_persistent_connection_with_no_pings(self):
        url = event_source_url(session())
        assert f"closeafter={CLOSE_AFTER_NO}" in url
        assert "ping=0" in url

    def test_an_unpushable_type_is_refused_locally(self):
        # The alternative is silence: the server simply never sends notifications
        # for a type it does not have, which looks exactly like nothing changing.
        with pytest.raises(UnknownPushTypeError, match="Widget"):
            event_source_url(session(), types=("Email", "Widget"), push_types=frozenset({"Email"}))

    def test_the_error_names_what_the_server_does_offer(self):
        with pytest.raises(UnknownPushTypeError) as excinfo:
            event_source_url(session(), types=("Widget",), push_types=frozenset({"Email"}))
        assert excinfo.value.unknown == ("Widget",)
        assert "Email" in str(excinfo.value)

    def test_a_server_offering_nothing_says_so(self):
        with pytest.raises(UnknownPushTypeError, match="no types at all"):
            event_source_url(session(), types=("Email",), push_types=frozenset())

    def test_types_are_unchecked_when_no_push_types_are_known(self):
        assert event_source_url(session(), types=("Anything",))

    def test_the_portable_ping_range_is_stated(self):
        # RFC 8620 §7.3 bounds what a server may clamp to, so anything in here is
        # honoured verbatim everywhere.
        assert MIN_PORTABLE_PING == 30
        assert MAX_PORTABLE_PING == 300


class TestParseEvent:
    def test_a_state_event_becomes_a_state_change(self):
        parsed = parse_event("state", '{"@type":"StateChange","changed":{"a":{"Email":"e1"}}}')
        assert isinstance(parsed, StateChange)
        assert parsed.states_for("a") == {"Email": "e1"}

    def test_a_ping_carries_the_interval_the_server_settled_on(self):
        parsed = parse_event("ping", '{"interval": 300}')
        assert parsed == Ping(interval=300)

    def test_a_ping_without_an_interval_is_still_a_ping(self):
        assert parse_event("ping", "{}") == Ping(interval=None)

    def test_a_non_integer_interval_is_discarded(self):
        assert parse_event("ping", '{"interval": "soon"}') == Ping(interval=None)

    def test_an_unknown_event_type_is_ignored_rather_than_fatal(self):
        # A stream may carry events this RFC does not name, and a client that dies
        # on one cannot be extended without breaking it.
        assert parse_event("something-else", "{}") is None

    def test_a_non_json_payload_raises(self):
        with pytest.raises(EventSourceError, match="not JSON"):
            parse_event("state", "not json at all")

    def test_a_json_scalar_payload_raises(self):
        with pytest.raises(EventSourceError, match="not a JSON object"):
            parse_event("state", "42")


class TestEventStream:
    def test_state_events_are_yielded(self):
        stream = EventStream.open()
        events = list(stream.feed(b'event: state\ndata: {"changed":{"a":{"Email":"e1"}}}\n\n'))
        assert isinstance(events[0], StateChange)

    def test_the_cursor_advances_on_a_state_event(self):
        stream = EventStream.open()
        list(stream.feed(b'id: 42\nevent: state\ndata: {"changed":{}}\n\n'))
        assert stream.last_event_id == "42"

    def test_a_ping_does_not_move_the_cursor(self):
        # RFC 8620 §7.3 forbids a ping from setting an event id. A client that
        # tracked "the last event I received" would resume from the ping and skip
        # every change that arrived before it.
        stream = EventStream.open()
        list(stream.feed(b'id: 42\nevent: state\ndata: {"changed":{}}\n\n'))
        list(stream.feed(b'event: ping\ndata: {"interval":300}\n\n'))
        assert stream.last_event_id == "42"

    def test_unknown_events_are_skipped(self):
        stream = EventStream.open()
        assert list(stream.feed(b"event: mystery\ndata: {}\n\n")) == []

    def test_the_retry_hint_is_exposed(self):
        stream = EventStream.open()
        list(stream.feed(b'retry: 5000\nevent: state\ndata: {"changed":{}}\n\n'))
        assert stream.retry == 5000

    def test_close_after_state_is_recorded_when_asked_for(self):
        stream = EventStream.open(close_after_state=True)
        list(stream.feed(b'event: state\ndata: {"changed":{}}\n\n'))
        assert stream.closed_after_state is True

    def test_close_after_state_is_not_recorded_otherwise(self):
        stream = EventStream.open()
        list(stream.feed(b'event: state\ndata: {"changed":{}}\n\n'))
        assert stream.closed_after_state is False

    def test_a_stream_can_be_opened_at_a_known_cursor(self):
        assert EventStream.open(last_event_id="7").last_event_id == "7"

    @pytest.mark.parametrize(
        "unsendable",
        [
            "ev日",  # not ASCII: httpx raised UnicodeEncodeError building the request
            " 43",  # leading whitespace: h11 refused the header on every reconnect
            "43\t",  # trailing whitespace, likewise
            "a\x0bb",  # a control character, likewise
        ],
    )
    def test_an_id_that_cannot_go_back_as_a_header_does_not_become_the_cursor(self, unsendable):
        # The cursor returns as Last-Event-ID. An id that cannot be sent there
        # left listen() unable to reconnect at all, for good, since the cursor
        # never changed after. Resuming from the last id that *can* be sent
        # costs a short replay instead.
        state = b'event: state\ndata: {"changed":{}}\n\n'
        stream = EventStream.open()
        list(stream.feed(b"id: 42\n" + state))
        list(stream.feed(f"id: {unsendable}\n".encode() + state))
        assert stream.parser.last_event_id == unsendable
        assert stream.last_event_id == "42"
        list(stream.feed(b"id: 44\n\n"))  # a bare checkpoint moves it on again
        assert stream.last_event_id == "44"

    def test_an_id_with_inner_spaces_is_a_legal_header_value(self):
        stream = EventStream.open()
        list(stream.feed(b'id: 4 2\nevent: state\ndata: {"changed":{}}\n\n'))
        assert stream.last_event_id == "4 2"


class TestHeaders:
    def test_a_cursor_becomes_a_resume_header(self):
        assert resume_headers("7") == {"Last-Event-ID": "7"}

    def test_no_cursor_sends_no_header(self):
        # Sending an empty one would be a claim to have seen event "".
        assert resume_headers("") == {}

    def test_the_stream_headers_ask_for_the_right_media_type(self):
        assert stream_headers()["Accept"] == "text/event-stream"
        assert stream_headers("7")["Last-Event-ID"] == "7"


class TestSubscriptionBuilding:
    def test_a_minimal_creation(self):
        assert new_subscription("dev1", "https://push.example.com/x") == {
            "deviceClientId": "dev1",
            "url": "https://push.example.com/x",
        }

    def test_a_verification_code_is_never_sent_on_create(self):
        # §7.2: it MUST be null or omitted, and the server rejects a guess.
        assert "verificationCode" not in new_subscription("dev1", "https://push.example.com/x")

    def test_optional_fields_are_included_when_given(self):
        creation = new_subscription(
            "dev1",
            "https://push.example.com/x",
            types=["Email"],
            keys={"p256dh": "abc", "auth": "def"},
            expires="2026-01-01T00:00:00Z",
        )
        assert creation["types"] == ["Email"]
        assert creation["keys"] == {"p256dh": "abc", "auth": "def"}
        assert creation["expires"] == "2026-01-01T00:00:00Z"

    def test_an_insecure_url_is_refused_locally(self):
        # The payload is a notification about someone's mailbox.
        with pytest.raises(InsecurePushUrlError, match="https://"):
            new_subscription("dev1", "http://push.example.com/x")

    def test_check_push_url_accepts_https(self):
        check_push_url("https://push.example.com/x")

    def test_the_update_patches_are_single_property(self):
        assert verification_update("code") == {"verificationCode": "code"}
        assert renewal_update("2026-01-01T00:00:00Z") == {"expires": "2026-01-01T00:00:00Z"}


class TestOwnership:
    def test_only_this_devices_subscriptions_are_returned(self):
        # §7.2.2: the same credentials may be in use on another device, and its
        # subscription is not ours to revoke.
        subscriptions = [
            PushSubscription(id="p1", deviceClientId="mine"),
            PushSubscription(id="p2", deviceClientId="theirs"),
        ]
        assert [s.id for s in mine(subscriptions, "mine")] == ["p1"]

    def test_nothing_matches_when_none_are_ours(self):
        assert mine([PushSubscription(id="p1", deviceClientId="theirs")], "mine") == []


class TestPendingVerification:
    def test_a_code_can_be_claimed_after_the_create_returns(self):
        pending = PendingVerification()
        pending.record(PushVerification(pushSubscriptionId="p1", verificationCode="code"))
        assert pending.claim("p1") == "code"

    def test_a_code_arriving_first_is_still_found(self):
        # The race in §7.2.3: the push can beat the /set response. A client that
        # only looks after the create returns misses it and waits forever.
        pending = PendingVerification()
        pending.record(PushVerification(pushSubscriptionId="p1", verificationCode="early"))
        assert "p1" in pending
        assert pending.claim("p1") == "early"

    def test_claiming_consumes_the_code(self):
        pending = PendingVerification()
        pending.record(PushVerification(pushSubscriptionId="p1", verificationCode="code"))
        pending.claim("p1")
        assert pending.claim("p1") is None
        assert len(pending) == 0

    def test_an_unknown_subscription_has_no_code(self):
        assert PendingVerification().claim("p1") is None

    def test_an_incomplete_verification_is_ignored(self):
        pending = PendingVerification()
        pending.record(PushVerification(pushSubscriptionId="p1"))
        pending.record(PushVerification(verificationCode="orphan"))
        assert len(pending) == 0

    def test_the_oldest_unclaimed_code_is_evicted_at_the_cap(self):
        # Whatever feeds record() may be reachable by a sender who can mint
        # subscription ids, so the codes are bounded - and the one that has
        # waited longest belongs to a create that is not coming back for it.
        pending = PendingVerification()
        for index in range(PendingVerification.MAX_PENDING + 1):
            pending.record(PushVerification(pushSubscriptionId=f"p{index}", verificationCode="c"))
        assert len(pending) == PendingVerification.MAX_PENDING
        assert "p0" not in pending
        assert f"p{PendingVerification.MAX_PENDING}" in pending


class TestWebSocketCapability:
    def test_the_rfc_example_parses(self):
        capability = WebSocketCapability.of(
            {"url": "wss://server.example.com/jmap/ws/", "supportsPush": True}
        )
        assert capability.url == "wss://server.example.com/jmap/ws/"
        assert capability.supports_push is True
        assert capability.is_secure is True

    def test_push_is_off_unless_advertised(self):
        # A socket that carries API calls but no notifications is a legal server.
        assert WebSocketCapability.of({"url": "wss://x/"}).supports_push is False

    def test_a_plaintext_endpoint_is_flagged(self):
        # RFC 8887 §4.2 requires TLS, so this server cannot be used safely.
        assert WebSocketCapability.of({"url": "ws://x/"}).is_secure is False

    def test_a_missing_url_is_not_secure(self):
        assert WebSocketCapability.of({}).is_secure is False

    def test_a_malformed_object_degrades(self):
        assert WebSocketCapability.of("nonsense").url is None

    def test_the_spec_adds_no_methods(self):
        assert WEBSOCKET.methods == ()
        assert WEBSOCKET.urn == WEBSOCKET_URN


class TestVapid:
    def test_the_key_is_read(self):
        assert VapidCapability.of({"applicationServerKey": "BKey"}).application_server_key == "BKey"

    def test_a_malformed_object_degrades(self):
        assert VapidCapability.of(7).application_server_key is None

    def test_a_rotation_is_detected(self):
        # RFC 9749 §5: the server destroys subscriptions tied to the old key and
        # notifications just stop. Nothing raises.
        assert vapid_key_rotated(VapidCapability(applicationServerKey="new"), "old") is True

    def test_an_unchanged_key_is_not_a_rotation(self):
        assert vapid_key_rotated(VapidCapability(applicationServerKey="same"), "same") is False

    def test_an_unknown_key_on_either_side_is_not_a_rotation(self):
        # An absence is not a change, and recreating on that basis would loop.
        assert vapid_key_rotated(VapidCapability(applicationServerKey="new"), None) is False
        assert vapid_key_rotated(VapidCapability(), "old") is False

    def test_the_key_is_read_off_a_session(self):
        advertised = session(**{VAPID_URN: {"applicationServerKey": "BKey"}})
        assert application_server_key(advertised) == "BKey"

    def test_a_session_without_vapid_has_no_key(self):
        assert application_server_key(session()) is None

    def test_needs_recreating_follows_the_session(self):
        advertised = session(**{VAPID_URN: {"applicationServerKey": "new"}})
        assert needs_recreating(advertised, "old") is True
        assert needs_recreating(advertised, "new") is False

    def test_the_spec_adds_no_methods(self):
        assert VAPID.methods == ()
        assert VAPID.urn == VAPID_URN


class TestReconnectDelay:
    def _listener_with_retry(self, milliseconds: int) -> PushListener:
        listener = PushListener("https://x/es", close_after_state=False)
        stream = listener.new_stream()
        list(stream.feed(f"retry: {milliseconds}\n\n".encode()))
        listener.absorb(stream)
        return listener

    def test_the_server_retry_is_honoured(self):
        assert self._listener_with_retry(5000).delay() == 5.0

    def test_an_absurd_retry_is_capped(self):
        # retry: has no upper bound in the spec; honouring a huge one would let
        # a single frame disable push forever while the client looks alive.
        listener = self._listener_with_retry(999_999_999_999)
        assert listener.delay() == 600.0

    def test_consecutive_failures_back_off_exponentially(self):
        listener = PushListener("https://x/es", close_after_state=False)
        first = listener.delay()
        listener.note_failure()
        second = listener.delay()
        listener.note_failure()
        third = listener.delay()
        assert first < second < third

    def test_backoff_is_capped(self):
        listener = PushListener("https://x/es", close_after_state=False)
        for _ in range(64):
            listener.note_failure()
        assert listener.delay() == 600.0

    def test_a_delivery_resets_the_backoff(self):
        listener = PushListener("https://x/es", close_after_state=False)
        resting = listener.delay()
        listener.note_failure()
        listener.note_failure()
        listener.note_delivery()
        assert listener.delay() == resting


class TestTheEventSourceDeadline:
    """An event source is idle by design, so the HTTP client's read timeout is
    exactly the wrong deadline to inherit: it bounds how long a *healthy* stream
    may wait for the next change. httpx defaults it to five seconds."""

    def test_no_ping_means_no_deadline(self):
        # Nothing was promised, so nothing can be concluded from silence.
        listener = PushListener("https://x/es", close_after_state=False, ping=0)
        assert listener.read_timeout is None

    def test_a_ping_interval_becomes_the_deadline(self):
        # §7.3: the server sends an empty event every `ping` seconds, so silence
        # past that is evidence rather than patience.
        listener = PushListener("https://x/es", close_after_state=False, ping=60)
        assert listener.read_timeout == 60 + PING_TIMEOUT_SLACK

    def test_the_deadline_leaves_room_for_a_late_ping(self):
        listener = PushListener("https://x/es", close_after_state=False, ping=60)
        assert listener.read_timeout is not None
        assert listener.read_timeout > 60

    def test_a_request_below_the_portable_floor_still_allows_for_the_clamp(self):
        # The regression this exists for. §7.3 lets a server set a minimum of up
        # to 30s, so a 5s request may legally become a 30s interval - and a client
        # that hangs up at 5s turns two conformant halves into a push feature that
        # never delivers anything. Stalwart clamps to exactly 30.
        listener = PushListener("https://x/es", close_after_state=False, ping=5)
        assert listener.read_timeout == MIN_PORTABLE_PING + PING_TIMEOUT_SLACK

    def test_a_request_above_the_portable_ceiling_needs_no_allowance(self):
        # A server may only clamp this one *down*, which makes pings arrive
        # sooner than the deadline assumes - never later.
        listener = PushListener("https://x/es", close_after_state=False, ping=600)
        assert listener.read_timeout == 600 + PING_TIMEOUT_SLACK

    def test_a_negative_ping_is_treated_as_none(self):
        listener = PushListener("https://x/es", close_after_state=False, ping=-1)
        assert listener.read_timeout is None

    def test_only_the_read_leg_is_replaced(self):
        # Connecting to an event source should fail as fast as connecting to
        # anything else; it is the waiting that differs.
        base = httpx.Timeout(7.0, connect=3.0)
        timeout = stream_timeout(base, None)
        assert (timeout.connect, timeout.write, timeout.pool) == (3.0, 7.0, 7.0)
        assert timeout.read is None

    def test_a_finite_read_deadline_is_carried_through(self):
        timeout = stream_timeout(httpx.Timeout(5.0), 40.0)
        assert timeout.read == 40.0
