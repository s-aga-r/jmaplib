"""Typed responses and the per-type method surfaces."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic.alias_generators import to_camel

from jmap.api.entity import (
    Changeable,
    Copyable,
    EntityBase,
    Gettable,
    Queryable,
    QueryChangeable,
    Settable,
    entity_class,
    entity_for,
)
from jmap.batch import Batch
from jmap.capabilities.core import CORE, CORE_URN
from jmap.capabilities.mail import (
    MAIL,
    MAIL_URN,
    SUBMISSION,
    SUBMISSION_URN,
    VACATION,
    VACATION_URN,
)
from jmap.capabilities.registry import ActiveCapabilities, Registry
from jmap.capabilities.spec import DataTypeSpec, MethodKind
from jmap.core.errors import JMAPError
from jmap.core.ids import CreationRef, Id
from jmap.core.session import Session
from jmap.models.base import UNSET, JMAPObject
from jmap.models.mail.objects import Email, Mailbox
from jmap.models.responses import (
    AddedItem,
    ChangesResponse,
    CopyResponse,
    GetResponse,
    QueryChangesResponse,
    QueryResponse,
    SetResponse,
    parse_set_errors,
)


def active_capabilities() -> ActiveCapabilities:
    registry = Registry()
    for spec in (CORE, MAIL, SUBMISSION, VACATION):
        registry.register(spec)
    session = Session.from_wire(
        {
            "capabilities": {
                CORE_URN: {"maxCallsInRequest": 16},
                MAIL_URN: {},
                SUBMISSION_URN: {},
                VACATION_URN: {},
            },
            "accounts": {"a": {"name": "alice"}},
            "primaryAccounts": {CORE_URN: "a", MAIL_URN: "a", SUBMISSION_URN: "a"},
        }
    )
    return registry.resolve(session, Id("a"))


@pytest.fixture
def batch() -> Batch:
    return Batch(active_capabilities())


def email_entity(batch: Batch) -> Any:
    data_type = MAIL.data_type("Email")
    assert data_type is not None
    return entity_for(batch, MAIL, data_type)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class TestGetResponse:
    def test_items_is_aliased_from_list(self):
        # A field literally named `list` would shadow the builtin inside the
        # class body and break every annotation after it.
        response = GetResponse[Email].model_validate(
            {"accountId": "a", "state": "s", "list": [{"id": "m1"}], "notFound": ["m2"]}
        )
        assert response.items[0].id == "m1"
        assert response.not_found == ["m2"]

    def test_items_are_the_declared_model(self):
        response = GetResponse[Email].model_validate({"list": [{"id": "m1", "subject": "Hi"}]})
        assert isinstance(response.items[0], Email)
        assert response.items[0].subject == "Hi"

    def test_defaults_are_empty_not_none(self):
        response = GetResponse[Email].model_validate({})
        assert response.items == []
        assert response.not_found == []


class TestChangesResponse:
    def test_parses_the_three_id_lists(self):
        response = ChangesResponse.model_validate(
            {
                "oldState": "s1",
                "newState": "s2",
                "hasMoreChanges": True,
                "created": ["a"],
                "updated": ["b"],
                "destroyed": ["c"],
            }
        )
        assert response.has_more_changes is True
        assert (response.created, response.updated, response.destroyed) == (["a"], ["b"], ["c"])

    def test_has_more_changes_defaults_false(self):
        assert ChangesResponse.model_validate({}).has_more_changes is False


class TestSetResponse:
    def test_half_success_is_representable(self):
        # created and notCreated arrive together; raising would discard the half
        # that worked.
        response = SetResponse[Email].model_validate(
            {"created": {"d1": {"id": "M1"}}, "notCreated": {"d2": {"type": "overQuota"}}}
        )
        assert response.created_id("d1") == "M1"
        assert response.has_errors
        assert response.creation_errors["d2"].type == "overQuota"

    def test_a_null_update_is_a_success(self):
        # RFC 8620 §5.3: null means "changed exactly what you asked, nothing more".
        response = SetResponse[Email].model_validate({"updated": {"m1": None}})
        assert response.updated == {"m1": None}
        assert not response.has_errors

    def test_created_id_of_an_unknown_creation(self):
        assert SetResponse[Email].model_validate({}).created_id("nope") is None

    def test_created_id_when_the_object_has_none(self):
        response = SetResponse[Email].model_validate({"created": {"d1": {}}})
        assert response.created_id("d1") is None

    def test_update_and_destroy_errors(self):
        response = SetResponse[Email].model_validate(
            {
                "notUpdated": {"m1": {"type": "forbidden"}},
                "notDestroyed": {"m2": {"type": "notFound"}},
            }
        )
        assert response.update_errors["m1"].type == "forbidden"
        assert response.destroy_errors["m2"].type == "notFound"
        assert response.has_errors

    def test_no_errors_is_the_clean_case(self):
        assert not SetResponse[Email].model_validate({"created": {}}).has_errors

    def test_the_rfcs_null_for_an_empty_category_parses(self):
        # RFC 8620 §5.3 types all six maps as nullable - "null if no Foo objects
        # were successfully created", and so on. Rejecting null turned every
        # conformant /set that did not touch all six categories into a
        # malformedResult, for a write the server had already applied.
        response = SetResponse[Email].model_validate(
            {
                "accountId": "a",
                "oldState": "s1",
                "newState": "s2",
                "created": None,
                "updated": {"m1": None},
                "destroyed": None,
                "notCreated": None,
                "notUpdated": None,
                "notDestroyed": None,
            }
        )
        assert response.updated == {"m1": None}
        assert response.created == {}
        assert response.destroyed == []
        assert not response.has_errors
        assert response.creation_errors == {}


class TestQueryResponses:
    def test_query(self):
        response = QueryResponse.model_validate(
            {"queryState": "q", "canCalculateChanges": True, "position": 3, "ids": ["m1"]}
        )
        assert response.can_calculate_changes is True
        assert response.position == 3

    def test_query_defaults(self):
        response = QueryResponse.model_validate({})
        assert response.ids == []
        assert response.position == 0
        assert response.can_calculate_changes is False

    def test_query_changes_added_items_carry_indices(self):
        # The indices describe the list after removals, which is why order of
        # application matters.
        response = QueryChangesResponse.model_validate(
            {"removed": ["m9"], "added": [{"id": "m1", "index": 0}]}
        )
        assert response.removed == ["m9"]
        assert response.added[0].index == 0

    def test_added_item_model(self):
        assert AddedItem.model_validate({"id": "m1", "index": 2}).index == 2


class TestCopyResponse:
    def test_created_and_errors(self):
        response = CopyResponse[Email].model_validate(
            {
                "fromAccountId": "b",
                "created": {"c1": {"id": "M9"}},
                "notCreated": {"c2": {"type": "alreadyExists", "existingId": "M8"}},
            }
        )
        assert response.from_account_id == "b"
        assert response.creation_errors["c2"].existing_id == "M8"

    def test_the_rfcs_null_for_an_empty_category_parses(self):
        # RFC 8620 §5.4: both maps are nullable, exactly as for /set.
        response = CopyResponse[Email].model_validate(
            {
                "fromAccountId": "b",
                "accountId": "a",
                "oldState": None,
                "newState": "s2",
                "created": {"c1": {"id": "M9"}},
                "notCreated": None,
            }
        )
        assert response.created["c1"].id == "M9"
        assert response.not_created == {}


class TestParseSetErrors:
    def test_empty(self):
        assert parse_set_errors({}) == {}

    def test_carries_the_property_list(self):
        errors = parse_set_errors({"d1": {"type": "invalidProperties", "properties": ["subject"]}})
        assert errors["d1"].properties == ("subject",)


# --------------------------------------------------------------------------- #
# Entity composition
# --------------------------------------------------------------------------- #


class TestComposition:
    """The surface must match what the capability actually declares."""

    def test_email_gets_all_six_shapes(self, batch):
        entity = email_entity(batch)
        for method in ("get", "changes", "query", "query_changes", "set", "copy"):
            assert hasattr(entity, method), method

    def test_thread_has_no_query_and_no_set(self, batch):
        # Threads are derived rather than stored, so misuse should be an
        # AttributeError here, not an unknownMethod from the server.
        data_type = MAIL.data_type("Thread")
        assert data_type is not None
        entity = entity_for(batch, MAIL, data_type)
        assert hasattr(entity, "get")
        assert hasattr(entity, "changes")
        assert not hasattr(entity, "query")
        assert not hasattr(entity, "set")

    def test_mailbox_has_no_copy(self, batch):
        data_type = MAIL.data_type("Mailbox")
        assert data_type is not None
        entity = entity_for(batch, MAIL, data_type)
        assert hasattr(entity, "set")
        assert not hasattr(entity, "copy")

    def test_vacation_response_is_get_and_set_only(self, batch):
        data_type = VACATION.data_type("VacationResponse")
        assert data_type is not None
        entity = entity_for(batch, VACATION, data_type)
        assert hasattr(entity, "get")
        assert hasattr(entity, "set")
        assert not hasattr(entity, "query")

    def test_a_type_with_no_methods_falls_back_to_the_base(self, batch):
        entity = entity_for(batch, MAIL, DataTypeSpec(name="Unknown"))
        assert type(entity) is entity_class(frozenset())
        assert entity_class(frozenset()).__bases__ == (EntityBase,)
        assert not hasattr(entity, "get")

    def test_an_irregular_get_comes_from_its_own_builder(self, batch):
        # SearchSnippet/get is CUSTOM, not a standard /get: it takes emailIds.
        data_type = MAIL.data_type("SearchSnippet")
        assert data_type is not None
        entity = entity_for(batch, MAIL, data_type)
        assert not isinstance(entity, Gettable)
        assert hasattr(entity, "get")

    def test_classes_are_cached_per_shape(self):
        first = entity_class(frozenset({MethodKind.GET}))
        second = entity_class(frozenset({MethodKind.GET}))
        assert first is second

    def test_a_type_that_queries_without_query_changes_has_no_query_changes(self):
        # One mixin supplied both, so every type with /query grew .query_changes
        # - SieveScript and the legacy Contact among them, whose only answer was
        # an UnsupportedMethodError at the call.
        entity = entity_class(frozenset({MethodKind.QUERY}))
        assert hasattr(entity, "query")
        assert not hasattr(entity, "query_changes")
        tracked = entity_class(frozenset({MethodKind.QUERY, MethodKind.QUERY_CHANGES}))
        assert hasattr(tracked, "query_changes")

    def test_mixin_bases_are_what_they_claim(self, batch):
        entity = email_entity(batch)
        for mixin in (Gettable, Changeable, Queryable, QueryChangeable, Settable, Copyable):
            assert isinstance(entity, mixin)

    def test_repr_names_the_type(self, batch):
        assert "Email" in repr(email_entity(batch))


class TestBuilders:
    def test_get_emits_snake_to_camel(self, batch):
        handle = email_entity(batch).get(ids=[Id("m1")], properties=["subject"])
        assert handle.call.name == "Email/get"
        assert handle.call.arguments["ids"] == ["m1"]
        assert handle.call.arguments["properties"] == ["subject"]

    def test_unset_arguments_are_omitted_entirely(self, batch):
        handle = email_entity(batch).get(ids=[Id("m1")])
        assert "properties" not in handle.call.arguments

    def test_explicit_none_survives_as_json_null(self, batch):
        # ids=None means "every record", which is not the same as omitting it.
        handle = email_entity(batch).get(ids=None)
        assert handle.call.arguments["ids"] is None

    def test_extra_arguments_pass_through(self, batch):
        handle = email_entity(batch).get(ids=[Id("m1")], fetchTextBodyValues=True)
        assert handle.call.arguments["fetchTextBodyValues"] is True

    def test_changes(self, batch):
        handle = email_entity(batch).changes(since_state="s1", max_changes=10)
        assert handle.call.name == "Email/changes"
        assert handle.call.arguments["sinceState"] == "s1"
        assert handle.call.arguments["maxChanges"] == 10

    def test_set(self, batch):
        handle = email_entity(batch).set(
            create={"d1": {"subject": "Hi"}}, if_in_state="s1", destroy=[Id("m9")]
        )
        assert handle.call.arguments["ifInState"] == "s1"
        assert handle.call.arguments["create"] == {"d1": {"subject": "Hi"}}
        assert handle.call.arguments["destroy"] == ["m9"]

    def test_query(self, batch):
        handle = email_entity(batch).query(
            filter={"inMailbox": "mb1"}, limit=10, calculate_total=True
        )
        assert handle.call.arguments["filter"] == {"inMailbox": "mb1"}
        assert handle.call.arguments["calculateTotal"] is True

    def test_query_changes(self, batch):
        handle = email_entity(batch).query_changes(since_query_state="q1", up_to_id=Id("m5"))
        assert handle.call.arguments["sinceQueryState"] == "q1"
        assert handle.call.arguments["upToId"] == "m5"

    def test_copy(self, batch):
        handle = email_entity(batch).copy(
            from_account_id=Id("b"),
            create={"c1": {"id": "m1"}},
            on_success_destroy_original=True,
        )
        assert handle.call.arguments["fromAccountId"] == "b"
        assert handle.call.arguments["onSuccessDestroyOriginal"] is True

    def test_a_back_reference_can_be_passed_as_ids(self, batch):
        query = email_entity(batch).query()
        handle = email_entity(batch).get(ids=query.ref_ids())
        assert handle.call.to_wire_arguments()["#ids"]["path"] == "/ids"


def entity(batch: Batch, type_name: str) -> Any:
    data_type = MAIL.data_type(type_name)
    assert data_type is not None
    return entity_for(batch, MAIL, data_type)


def problems(excinfo: pytest.ExceptionInfo[ValidationError]) -> list[tuple[Any, ...]]:
    """Each failure's location and type, for comparing in one assertion."""
    return [(error["loc"], error["type"]) for error in excinfo.value.errors()]


class TestArgumentsAreChecked:
    """Every builder validates its arguments before queueing the call, so a
    mistake raises where it was made instead of as ``invalidArguments``."""

    def test_a_string_is_not_a_list_of_ids(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).get(ids="m1")
        assert problems(excinfo) == [(("ids",), "sequence_str")]

    def test_the_error_names_the_type_and_the_builder(self, batch):
        with pytest.raises(ValidationError, match=r"validation error for Email\.get") as excinfo:
            email_entity(batch).get(ids="m1")
        # A ValueError, like the library's other argument errors, but not a
        # JMAPError: it is a bug in the call, not something the server did.
        assert isinstance(excinfo.value, ValueError)
        assert not isinstance(excinfo.value, JMAPError)

    def test_an_element_is_located_by_its_index(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).get(ids=["m1", 5])
        assert {loc[:2] for loc, _ in problems(excinfo)} == {("ids", 1)}

    def test_every_argument_at_fault_is_reported_at_once(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).query(limit=-5, calculate_total="yes")
        assert problems(excinfo) == [
            (("limit",), "greater_than_equal"),
            (("calculate_total",), "bool_type"),
        ]

    @pytest.mark.parametrize("limit", [True, 2.0, "5", 2**53])
    def test_a_limit_is_an_unsigned_int_and_nothing_else(self, batch, limit):
        with pytest.raises(ValidationError):
            email_entity(batch).query(limit=limit)

    def test_a_position_may_be_negative(self, batch):
        # RFC 8620 §5.5: a negative position counts back from the end.
        assert email_entity(batch).query(position=-5).call.arguments["position"] == -5

    def test_a_sort_is_a_list_of_comparators(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).query(sort="receivedAt")
        assert problems(excinfo) == [(("sort",), "sequence_str")]

    def test_any_mapping_is_a_filter(self, batch):
        handle = email_entity(batch).query(filter=MappingProxyType({"inMailbox": "mb1"}))
        assert handle.call.arguments["filter"] == {"inMailbox": "mb1"}

    def test_a_list_is_not_a_filter(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).query(filter=["inMailbox"])
        assert problems(excinfo) == [(("filter",), "dict_type")]

    def test_a_tuple_of_ids_is_a_sequence_of_ids(self, batch):
        handle = email_entity(batch).get(ids=("m1", CreationRef("d1")))
        assert handle.call.to_wire_arguments()["ids"] == ["m1", "#d1"]

    def test_a_state_is_a_string(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).changes(since_state=None)
        assert problems(excinfo) == [(("since_state",), "string_type")]

    def test_max_changes_must_be_above_zero_for_changes(self, batch):
        # RFC 8620 §5.2: the server MUST reject anything else.
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).changes(since_state="s1", max_changes=0)
        assert problems(excinfo) == [(("max_changes",), "greater_than")]

    def test_but_query_changes_puts_no_floor_on_it(self, batch):
        handle = email_entity(batch).query_changes(since_query_state="q1", max_changes=0)
        assert handle.call.arguments["maxChanges"] == 0

    def test_a_patch_is_a_mapping(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).set(update={"m1": "subject"})
        assert problems(excinfo) == [(("update", "m1"), "dict_type")]

    def test_destroy_is_a_list_of_ids(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).set(destroy="m1")
        assert problems(excinfo) == [(("destroy",), "sequence_str")]

    def test_an_object_to_create_is_a_mapping_or_a_model(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).set(create={"d1": "Hi"})
        assert {loc[:2] for loc, _ in problems(excinfo)} == {("create", "d1")}

    def test_copy_checks_its_arguments_too(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).copy(from_account_id=5, create={})
        assert problems(excinfo) == [(("from_account_id",), "string_type")]

    def test_query_changes_checks_its_arguments_too(self, batch):
        with pytest.raises(ValidationError) as excinfo:
            email_entity(batch).query_changes(since_query_state="q1", up_to_id=5)
        assert problems(excinfo) == [(("up_to_id",), "string_type")]

    def test_a_missing_argument_is_a_type_error_naming_the_call(self, batch):
        with pytest.raises(TypeError, match=r"Email\.changes\(\): .*since_state"):
            email_entity(batch).changes()

    def test_a_positional_argument_is_a_type_error(self, batch):
        with pytest.raises(TypeError, match=r"Email\.get\(\)"):
            email_entity(batch).get(["m1"])

    def test_an_explicit_unset_is_still_omitted(self, batch):
        assert "ids" not in email_entity(batch).get(ids=UNSET).call.arguments


class TestBackReferencesAreForwarded:
    """RFC 8620 §3.7 lets any argument be a back-reference, and its value exists
    only once the server resolves it - so a builder forwards one unchecked."""

    @pytest.mark.parametrize(
        ("builder", "arguments"),
        [
            ("get", {"properties": "/updatedProperties"}),
            ("changes", {"since_state": "/state"}),
            ("query", {"limit": "/total"}),
            ("query_changes", {"since_query_state": "/queryState"}),
            ("set", {"destroy": "/ids", "if_in_state": "/state"}),
        ],
    )
    def test_a_reference_goes_through_unchecked(self, batch, builder, arguments):
        source = email_entity(batch).query()
        refs = {name: source.ref(path) for name, path in arguments.items()}
        handle = getattr(email_entity(batch), builder)(**refs)
        wire = handle.call.to_wire_arguments()
        assert all(wire[f"#{to_camel(name)}"]["path"] == path for name, path in arguments.items())


class TestModelsAsInput:
    """A typed model is accepted wherever its wire object is."""

    def test_a_model_is_an_object_to_create(self, batch):
        handle = entity(batch, "Mailbox").set(create={"k": Mailbox(name="Receipts")})
        # Only the fields it was given: RFC 8620 §5.3 has the server default the
        # rest, and a null would say something else.
        assert handle.call.to_wire_arguments()["create"] == {"k": {"name": "Receipts"}}

    def test_an_explicit_null_is_kept(self, batch):
        handle = entity(batch, "Mailbox").set(create={"k": Mailbox(name="R", parent_id=None)})
        assert handle.call.to_wire_arguments()["create"]["k"] == {"name": "R", "parentId": None}

    def test_a_model_is_an_object_to_copy(self, batch):
        handle = email_entity(batch).copy(from_account_id="b", create={"c1": Email(id="m1")})
        assert handle.call.to_wire_arguments()["create"] == {"c1": {"id": "m1"}}

    def test_a_raw_object_is_one_too(self, batch):
        handle = entity(batch, "Mailbox").set(create={"k": JMAPObject({"name": "R"})})
        assert handle.call.to_wire_arguments()["create"] == {"k": {"name": "R"}}


class TestPaginationGuards:
    """RFC 8620 §5.5 makes these silent no-ops server-side, so reject locally."""

    def test_anchor_and_position_together_are_rejected(self):
        entity = email_entity(Batch(active_capabilities()))
        with pytest.raises(ValueError, match="not both"):
            entity.query(anchor=Id("m1"), position=5)

    def test_anchor_offset_without_an_anchor_is_rejected(self):
        entity = email_entity(Batch(active_capabilities()))
        with pytest.raises(ValueError, match="ignored without an `anchor`"):
            entity.query(anchor_offset=5)

    def test_anchor_with_offset_is_fine(self, batch):
        handle = email_entity(batch).query(anchor=Id("m1"), anchor_offset=5)
        assert handle.call.arguments["anchorOffset"] == 5

    def test_position_alone_is_fine(self, batch):
        assert email_entity(batch).query(position=5).call.arguments["position"] == 5

    def test_an_explicit_null_anchor_does_not_block_position(self, batch):
        # anchor=None means "no anchor", so position is still meaningful.
        handle = email_entity(batch).query(anchor=None, position=5)
        assert handle.call.arguments["position"] == 5
