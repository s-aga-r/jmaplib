"""The ``text/event-stream`` parser.

Every rule here is one a naive line-splitting parser gets wrong, and every one of
those is silent: a truncated payload, a resumption cursor pointing at the wrong
place, an event delivered twice.
"""

from __future__ import annotations

from jmap.push.sse import DEFAULT_EVENT_TYPE, ServerSentEvent, SSEParser


def events(*chunks: str) -> list[ServerSentEvent]:
    parser = SSEParser()
    return [event for chunk in chunks for event in parser.feed(chunk)]


class TestFraming:
    def test_a_blank_line_dispatches(self):
        assert events("data: hello\n\n") == [ServerSentEvent(type=DEFAULT_EVENT_TYPE, data="hello")]

    def test_nothing_is_dispatched_before_the_blank_line(self):
        assert events("data: hello\n") == []

    def test_the_event_type_is_carried(self):
        [event] = events("event: state\ndata: {}\n\n")
        assert event.type == "state"

    def test_an_event_with_no_type_is_a_message(self):
        [event] = events("data: x\n\n")
        assert event.type == DEFAULT_EVENT_TYPE

    def test_exactly_one_leading_space_is_framing(self):
        # `data:  x` has a real leading space in the value; `data: x` does not.
        assert events("data:  x\n\n")[0].data == " x"
        assert events("data:x\n\n")[0].data == "x"

    def test_a_field_with_no_colon_has_an_empty_value(self):
        # Legal, and the way a stream sends `data` with nothing in it.
        assert events("data\n\n")[0].data == ""

    def test_a_comment_dispatches_nothing(self):
        # Servers send bare `:` lines as keep-alives.
        assert events(": keep-alive\n\n") == []

    def test_an_unknown_field_is_ignored(self):
        # What lets the format be extended without breaking existing parsers.
        [event] = events("nonsense: whatever\ndata: x\n\n")
        assert event.data == "x"


class TestMultiLineData:
    def test_data_lines_are_joined_with_newlines(self):
        # A parser keeping only the last line truncates every multi-line payload.
        assert events("data: one\ndata: two\n\n")[0].data == "one\ntwo"

    def test_an_empty_data_line_is_a_blank_line_in_the_payload(self):
        assert events("data: one\ndata\ndata: three\n\n")[0].data == "one\n\nthree"

    def test_the_trailing_newline_is_not_doubled(self):
        # Joining with "\n" already puts one between lines; keeping a trailing one
        # would append a phantom blank line to every payload.
        assert events("data: one\n\n")[0].data == "one"


class TestDispatchRules:
    def test_an_event_with_no_data_is_not_delivered(self):
        # Which is what makes a bare `id:` line a checkpoint rather than an event.
        assert events("id: 5\n\n") == []

    def test_a_bare_id_still_moves_the_cursor(self):
        parser = SSEParser()
        list(parser.feed("id: 5\n\n"))
        assert parser.last_event_id == "5"

    def test_the_event_type_resets_between_events(self):
        # Otherwise a typed event silently retypes every plain one after it.
        first, second = events("event: state\ndata: a\n\ndata: b\n\n")
        assert first.type == "state"
        assert second.type == DEFAULT_EVENT_TYPE

    def test_the_type_resets_even_when_nothing_was_dispatched(self):
        parser = SSEParser()
        list(parser.feed("event: state\n\n"))
        [event] = parser.feed("data: b\n\n")
        assert event.type == DEFAULT_EVENT_TYPE


class TestTheCursor:
    def test_the_last_event_id_persists_across_events(self):
        # *Not* reset between events, unlike data and type. This is what makes the
        # JMAP ping rule work: a ping carries no id, so the cursor stays put.
        parser = SSEParser()
        list(parser.feed("id: 7\nevent: state\ndata: a\n\n"))
        [event] = parser.feed("event: ping\ndata: {}\n\n")
        assert event.last_event_id == "7"
        assert parser.last_event_id == "7"

    def test_an_id_containing_nul_is_ignored_entirely(self):
        parser = SSEParser()
        list(parser.feed("id: 3\ndata: a\n\n"))
        list(parser.feed("id: bad\x00id\ndata: b\n\n"))
        assert parser.last_event_id == "3"

    def test_an_empty_id_clears_the_cursor(self):
        # A legal way for a server to say "do not resume from here".
        parser = SSEParser()
        list(parser.feed("id: 3\ndata: a\n\n"))
        list(parser.feed("id\ndata: b\n\n"))
        assert parser.last_event_id == ""


class TestRetry:
    def test_a_numeric_retry_is_recorded(self):
        parser = SSEParser()
        [event] = parser.feed("retry: 2500\ndata: a\n\n")
        assert parser.retry == 2500
        assert event.retry == 2500

    def test_a_non_numeric_retry_is_ignored(self):
        parser = SSEParser()
        list(parser.feed("retry: soon\ndata: a\n\n"))
        assert parser.retry is None

    def test_a_negative_retry_is_ignored(self):
        # `-1` is not all digits, and a negative reconnection delay is meaningless.
        parser = SSEParser()
        list(parser.feed("retry: -1\ndata: a\n\n"))
        assert parser.retry is None

    def test_the_retry_persists_for_later_events(self):
        parser = SSEParser()
        list(parser.feed("retry: 100\ndata: a\n\n"))
        [event] = parser.feed("data: b\n\n")
        assert event.retry == 100


class TestLineTerminators:
    def test_bare_lf(self):
        assert events("data: a\n\n")[0].data == "a"

    def test_bare_cr(self):
        assert events("data: a\r\r")[0].data == "a"

    def test_crlf(self):
        assert events("data: a\r\n\r\n")[0].data == "a"

    def test_a_crlf_split_across_chunks_is_one_terminator(self):
        # The trap: a CR ending one read and an LF starting the next. Treating them
        # as two terminators dispatches the event early, with an empty payload.
        assert events("data: a\r", "\n\r\n") == [ServerSentEvent(data="a")]

    def test_a_cr_followed_by_data_in_the_next_chunk_ends_its_line(self):
        assert events("data: a\r", "data: b\r\r")[0].data == "a\nb"

    def test_mixed_terminators_in_one_stream(self):
        first, second = events("data: a\n\ndata: b\r\n\r\n")
        assert (first.data, second.data) == ("a", "b")


class TestChunking:
    def test_a_field_split_mid_name(self):
        assert events("da", "ta: hello\n\n")[0].data == "hello"

    def test_a_field_split_mid_value(self):
        assert events("data: hel", "lo\n\n")[0].data == "hello"

    def test_an_event_split_across_three_chunks(self):
        assert events("event: sta", "te\ndata: {}", "\n\n")[0].type == "state"

    def test_byte_input_is_decoded_as_utf8(self):
        parser = SSEParser()
        [event] = parser.feed_bytes("data: café\n\n".encode())
        assert event.data == "café"

    def test_several_events_in_one_chunk(self):
        assert [event.data for event in events("data: a\n\ndata: b\n\ndata: c\n\n")] == [
            "a",
            "b",
            "c",
        ]
