"""The ``text/event-stream`` parser.

Every rule here is one a naive line-splitting parser gets wrong, and every one of
those is silent: a truncated payload, a resumption cursor pointing at the wrong
place, an event delivered twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import jmap.push.sse as sse_module
from jmap.push.sse import (
    DEFAULT_EVENT_TYPE,
    EventOverflowError,
    ServerSentEvent,
    SSEParser,
)

if TYPE_CHECKING:
    import re


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

    def test_the_cursor_moves_only_when_its_event_is_dispatched(self):
        # An id line belongs to the event it sits in, and until the blank line
        # arrives that event has not been delivered. Moving the cursor at the id
        # line meant a connection dropping mid-event resumed *after* an event
        # nobody received - and a server replaying from Last-Event-ID never
        # sent it again. WHATWG's parser moves it at dispatch, too.
        parser = SSEParser()
        list(parser.feed("id: 4\nevent: state\ndata: {}\n\n"))
        list(parser.feed("id: 5\nevent: state\ndata: {}\n"))
        assert parser.last_event_id == "4"
        [event] = parser.feed("\n")
        assert event.last_event_id == "5"
        assert parser.last_event_id == "5"

    def test_a_resumed_stream_keeps_its_cursor_through_an_event_without_an_id(self):
        # A new connection starts from the cursor it was opened with, so its
        # first ping - which never carries an id - cannot wipe it.
        parser = SSEParser(last_event_id="7")
        [event] = parser.feed("event: ping\ndata: {}\n\n")
        assert event.last_event_id == "7"
        assert parser.last_event_id == "7"


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


class TestByteDecoding:
    def test_a_character_split_across_chunks_survives(self):
        # The transport chunks at arbitrary byte boundaries; a multi-byte
        # character cut in half must decode as one character, not crash.
        parser = SSEParser()
        payload = "data: café\n\n".encode()
        cut = payload.index("é".encode()) + 1  # inside the two-byte é
        collected = list(parser.feed_bytes(payload[:cut]))
        collected += list(parser.feed_bytes(payload[cut:]))
        assert [event.data for event in collected] == ["café"]

    def test_an_invalid_byte_becomes_the_replacement_character(self):
        # WHATWG decodes event streams with U+FFFD substitution; strict
        # decoding would turn one bad byte into a dead listener.
        parser = SSEParser()
        [event] = parser.feed_bytes(b"data: a\xffb\n\n")
        assert event.data == "a�b"

    def test_a_leading_bom_is_not_a_field_name(self):
        parser = SSEParser()
        [event] = parser.feed_bytes("﻿event: state\ndata: {}\n\n".encode())
        assert event.type == "state"


class TestHostileStreams:
    @staticmethod
    def _flood(parser: SSEParser, chunk: str) -> None:
        for _ in range(8):
            list(parser.feed(chunk))

    def test_an_endless_unterminated_line_overflows_loudly(self):
        # The connection is designed to stay open for days; without a bound a
        # single never-terminated data: line grows until the OS kills us.
        parser = SSEParser()
        with pytest.raises(EventOverflowError):
            self._flood(parser, "x" * (1 << 20))

    def test_endless_data_lines_with_no_dispatch_overflow_too(self):
        # Terminated lines that never see a blank line accumulate in _data;
        # they count toward the same cap as the raw buffer.
        parser = SSEParser()
        with pytest.raises(EventOverflowError):
            self._flood(parser, "data: " + "x" * (1 << 20) + "\n")

    def test_empty_data_lines_count_toward_the_cap(self, monkeypatch):
        # Each data line adds its value *and* the newline that joins it to the
        # next. Counting only the value let an endless run of bare `data` lines
        # grow _data forever at a tally of zero, behind a bound that looked
        # enforced. The cap is lowered so the flood is small enough to be quick,
        # and each chunk stays well under it so only accumulation can trip it.
        monkeypatch.setattr("jmap.push.sse.MAX_EVENT_CHARS", 1_000)
        parser = SSEParser()
        with pytest.raises(EventOverflowError):
            self._flood(parser, "data\n" * 100)

    def test_each_line_ending_is_found_without_rescanning_the_rest(self, monkeypatch):
        # Looking for CR and LF separately searched to the end of the buffer
        # for whichever one a stream never sends - CR, from any real server -
        # once per line. Quadratic: a 6 KB gzipped response inflated to 4 MB
        # cost 30 s of CPU. One pattern for both stops at the nearest, so the
        # searches add up to a single pass over the chunk.
        scanned: list[int] = []
        pattern = sse_module._LINE_ENDING

        class Spy:
            def search(self, text: str, position: int) -> re.Match[str] | None:
                match = pattern.search(text, position)
                scanned.append((match.end() if match else len(text)) - position)
                return match

        monkeypatch.setattr(sse_module, "_LINE_ENDING", Spy())
        chunk = "data: x\n" * 1_000
        list(SSEParser().feed(chunk))
        assert sum(scanned) <= len(chunk)

    def test_a_unicode_digit_retry_is_ignored_not_fatal(self):
        # '²'.isdigit() is true but int('²') raises; the crash would kill the
        # listener, so the field is ignored instead.
        parser = SSEParser()
        list(parser.feed("retry:²\n"))
        assert parser.retry is None

    def test_an_arabic_indic_retry_is_not_silently_honoured(self):
        parser = SSEParser()
        list(parser.feed("retry:٣\n"))
        assert parser.retry is None

    def test_an_absurdly_long_retry_is_ignored(self):
        parser = SSEParser()
        list(parser.feed("retry:" + "9" * 400 + "\n"))
        assert parser.retry is None
