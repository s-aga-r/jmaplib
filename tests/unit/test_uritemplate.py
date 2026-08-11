"""RFC 6570 Level 1 expansion, checked against the RFC's own examples and
against the session URLs a real Stalwart 0.16.17 hands out (RFC 8620 §2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jmap.core.uritemplate import (
    InvalidTemplateError,
    MissingVariableError,
    TemplateError,
    UnsupportedTemplateError,
    expand,
    variables,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "stalwart-0.16.17-session-bootstrap.json"
)
SESSION = json.loads(FIXTURE.read_text(encoding="utf-8"))

API_URL = SESSION["apiUrl"]
DOWNLOAD_URL = SESSION["downloadUrl"]
UPLOAD_URL = SESSION["uploadUrl"]
EVENT_SOURCE_URL = SESSION["eventSourceUrl"]


class TestRFC6570Examples:
    """§1.2 gives exactly two Level 1 examples; §3.2.2 gives the rest."""

    def test_level1_var(self):
        # RFC 6570 §1.2: var := "value"; {var} -> value
        assert expand("{var}", var="value") == "value"

    def test_level1_hello(self):
        # RFC 6570 §1.2: hello := "Hello World!"; {hello} -> Hello%20World%21
        assert expand("{hello}", hello="Hello World!") == "Hello%20World%21"

    def test_percent_is_itself_encoded(self):
        # RFC 6570 §3.2.2: half := "50%"; {half} -> 50%25
        assert expand("{half}", half="50%") == "50%25"

    def test_empty_value_expands_to_nothing(self):
        # RFC 6570 §3.2.2: O{empty}X -> OX
        assert expand("O{empty}X", empty="") == "OX"

    def test_slash_in_value_stays_in_its_segment(self):
        # The Level 1 counterpart of the §1.2 Level 2 example {+path}/here:
        # without the '+' operator the slashes are encoded, so path := "/foo/bar"
        # yields one segment, not three.
        assert expand("{path}/here", path="/foo/bar") == "%2Ffoo%2Fbar/here"

    def test_unreserved_set_is_left_alone(self):
        # RFC 6570 §1.5: unreserved = ALPHA / DIGIT / "-" / "." / "_" / "~".
        assert expand("{v}", v="aZ09-._~") == "aZ09-._~"

    def test_non_ascii_is_utf8_then_pct_encoded(self):
        # RFC 6570 §3.2.1 encodes to UTF-8 octets before pct-encoding.
        assert expand("{v}", v="über") == "%C3%BCber"


class TestStalwartSessionUrls:
    """The templates Stalwart 0.16.17 actually advertises."""

    def test_fixture_templates_are_level1(self):
        assert variables(DOWNLOAD_URL) == frozenset({"accountId", "blobId", "name", "type"})
        assert variables(UPLOAD_URL) == frozenset({"accountId"})
        assert variables(EVENT_SOURCE_URL) == frozenset({"types", "closeafter", "ping"})
        assert variables(API_URL) == frozenset()

    def test_download_url(self):
        assert expand(
            DOWNLOAD_URL,
            accountId="c",
            blobId="Gcafe1234",
            name="Q1 report.pdf",
            type="application/pdf",
        ) == (
            "https://localhost/jmap/download/c/Gcafe1234/Q1%20report.pdf?accept=application%2Fpdf"
        )

    def test_download_url_blob_id_containing_a_slash(self):
        # A slash in a blob id must not forge an extra path segment.
        url = expand(DOWNLOAD_URL, accountId="c", blobId="a/b", name="x", type="text/plain")
        assert url == "https://localhost/jmap/download/c/a%2Fb/x?accept=text%2Fplain"

    def test_download_url_filename_cannot_traverse(self):
        url = expand(
            DOWNLOAD_URL,
            accountId="c",
            blobId="G1",
            name="../../etc/passwd",
            type="text/plain",
        )
        assert "/../" not in url
        assert url.endswith("/G1/..%2F..%2Fetc%2Fpasswd?accept=text%2Fplain")

    def test_upload_url_keeps_its_trailing_slash(self):
        assert expand(UPLOAD_URL, accountId="cd41") == "https://localhost/jmap/upload/cd41/"

    def test_event_source_url_with_an_integer_ping(self):
        assert expand(EVENT_SOURCE_URL, types="*", closeafter="no", ping=30) == (
            "https://localhost/jmap/eventsource/?types=%2A&closeafter=no&ping=30"
        )

    def test_event_source_url_type_list_is_encoded(self):
        url = expand(EVENT_SOURCE_URL, types="Email,Mailbox", closeafter="state", ping=0)
        assert url == (
            "https://localhost/jmap/eventsource/?types=Email%2CMailbox&closeafter=state&ping=0"
        )

    def test_api_url_has_nothing_to_expand(self):
        assert expand(API_URL) == API_URL


class TestCallingConventions:
    @pytest.mark.parametrize(
        "template", ["", "https://localhost/jmap/", "{a}", DOWNLOAD_URL, EVENT_SOURCE_URL]
    )
    def test_variables_names_exactly_what_expand_requires(self, template):
        values = dict.fromkeys(variables(template), "v")
        expand(template, values)
        for name in values:
            without = {k: v for k, v in values.items() if k != name}
            with pytest.raises(MissingVariableError):
                expand(template, without)

    def test_mapping_form(self):
        assert expand("{a}/{b}", {"a": "1", "b": "2"}) == "1/2"

    def test_mapping_form_reaches_names_no_keyword_could(self):
        # RFC 6570 §2.3 varnames allow '.' and pct-encoding; neither is a legal
        # Python identifier, so these are only addressable via the mapping form.
        assert expand("{a.b}/{%41}", {"a.b": "x", "%41": "y"}) == "x/y"

    def test_extra_values_are_ignored(self):
        assert expand("{a}", a="1", b="2") == "1"

    def test_repeated_variable_expands_every_time(self):
        assert expand("{a}-{a}", a="x") == "x-x"
        assert variables("{a}-{a}") == frozenset({"a"})

    def test_empty_template(self):
        assert expand("") == ""
        assert variables("") == frozenset()

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            ("{a}tail", "1tail"),
            ("head{a}", "head1"),
            ("{a}", "1"),
            ("{a}{a}", "11"),
            ("head{a}tail", "head1tail"),
        ],
    )
    def test_literal_runs_around_expressions(self, template, expected):
        assert expand(template, a="1") == expected

    def test_integers_are_stringified(self):
        assert expand("{n}", n=0) == "0"
        assert expand("{n}", n=-1) == "-1"


class TestMissingVariable:
    def test_raises_instead_of_expanding_to_empty(self):
        # RFC 6570 §3.2.1 would give "OX"; we deliberately refuse.
        with pytest.raises(MissingVariableError):
            expand("O{undef}X", var="value")

    def test_names_the_variable_and_what_was_supplied(self):
        with pytest.raises(MissingVariableError) as excinfo:
            expand(DOWNLOAD_URL, accountId="c", blobId="G1", name="x")
        error = excinfo.value
        assert error.variable == "type"
        assert set(error.supplied) == {"accountId", "blobId", "name"}
        assert "'type'" in str(error)
        assert "accountId" in str(error)

    def test_reports_nothing_supplied(self):
        with pytest.raises(MissingVariableError, match=r"\(nothing\)"):
            expand("{a}")

    def test_first_missing_variable_wins(self):
        with pytest.raises(MissingVariableError) as excinfo:
            expand("{a}{b}")
        assert excinfo.value.variable == "a"


class TestUnsupportedLevels:
    @pytest.mark.parametrize("operator", ["+", "#", ".", "/", ";", "?", "&"])
    def test_level2_and_level3_operators(self, operator):
        template = f"https://h/{{{operator}var}}"
        with pytest.raises(UnsupportedTemplateError, match=re.escape(f"operator {operator!r}")):
            expand(template, var="value")

    @pytest.mark.parametrize("operator", ["=", ",", "!", "@", "|"])
    def test_operators_reserved_for_future_extensions(self, operator):
        # RFC 6570 §2.2 reserves these; guessing at their meaning is worse than
        # refusing, since a future Level would change what we produced.
        with pytest.raises(UnsupportedTemplateError, match=re.escape(f"operator {operator!r}")):
            expand(f"{{{operator}var}}", var="value")

    def test_variable_list(self):
        with pytest.raises(UnsupportedTemplateError, match="variable list"):
            expand("{x,y}", x="1024", y="768")

    def test_prefix_modifier(self):
        with pytest.raises(UnsupportedTemplateError, match="prefix modifier"):
            expand("{var:3}", var="value")

    def test_explode_modifier(self):
        with pytest.raises(UnsupportedTemplateError, match="explode modifier"):
            expand("{list*}", list="red")

    def test_carries_the_offending_expression(self):
        with pytest.raises(UnsupportedTemplateError) as excinfo:
            variables("https://h/{+base}/x")
        assert excinfo.value.expression == "+base"
        assert excinfo.value.feature == "operator '+'"
        assert excinfo.value.template == "https://h/{+base}/x"

    def test_variables_rejects_too(self):
        # variables() validates, so a bad session URL can be caught on receipt
        # rather than at the first download attempt.
        with pytest.raises(UnsupportedTemplateError):
            variables("{?query}")


class TestInvalidTemplates:
    @pytest.mark.parametrize(
        ("template", "reason"),
        [
            ("{", "unterminated '{'"),
            ("https://h/{accountId", "unterminated '{'"),
            ("}", "unmatched '}'"),
            ("a}b", "unmatched '}'"),
            ("{a}}", "unmatched '}'"),
            ("{}", "empty expression"),
            ("{{a}}", "'{' inside an expression"),
        ],
    )
    def test_brace_mismatch(self, template, reason):
        with pytest.raises(InvalidTemplateError, match=re.escape(reason)):
            variables(template)

    @pytest.mark.parametrize("name", ["a b", " ", "a-b", "a.", "a..b", "%zz", "%4", "a%"])
    def test_malformed_varnames(self, name):
        # A bare '.' is not here: it parses as the Level 3 operator, so it is
        # unsupported rather than invalid.
        with pytest.raises(InvalidTemplateError, match="is not a valid varname"):
            variables(f"{{{name}}}")

    @pytest.mark.parametrize("name", ["a", "A0", "_x", "a.b.c", "%41", "%41.b", "a_1"])
    def test_well_formed_varnames(self, name):
        assert variables(f"{{{name}}}") == frozenset({name})

    @pytest.mark.parametrize("template", ["{", "{+a}", "{a}"])
    def test_every_failure_is_a_template_error_and_a_value_error(self, template):
        # Callers that only care "this URL is unusable" catch one class.
        with pytest.raises(TemplateError, match="URI template") as excinfo:
            expand(template)
        assert isinstance(excinfo.value, ValueError)
