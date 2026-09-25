"""The batch refuses a sort the server said it cannot do (RFC 8620 §5.5).

Either mistake - a collation or a sort property the server did not advertise -
earns ``unsupportedSort`` for the whole query. The values here are Stalwart
0.16.17's.
"""

from __future__ import annotations

from typing import Any

import pytest

from jmap.batch import Batch
from jmap.capabilities.files import FILENODE_URN
from jmap.capabilities.mail import MAIL_URN
from jmap.core.errors import CapabilityFieldError
from jmap.core.limits import URN_CORE
from jmap.core.session import Session
from jmap.defaults import default_registry

SORTS = ["receivedAt", "size", "from", "to", "subject", "sentAt", "hasKeyword"]
COLLATIONS = ["i;ascii-numeric", "i;ascii-casemap", "i;unicode-casemap"]


def batch(
    *,
    sorts: list[str] | None = SORTS,
    collations: list[str] | None = COLLATIONS,
    shared_sorts: list[str] | None = None,
) -> Batch:
    core = {} if collations is None else {"collationAlgorithms": collations}
    mail = {} if sorts is None else {"emailQuerySortOptions": sorts}
    accounts: dict[str, Any] = {"a": {"name": "alice", "accountCapabilities": {MAIL_URN: mail}}}
    if shared_sorts is not None:
        shared = {MAIL_URN: {"emailQuerySortOptions": shared_sorts}}
        accounts["s"] = {"name": "team", "accountCapabilities": shared}
    session = Session.from_wire(
        {
            "capabilities": {URN_CORE: core, MAIL_URN: {}},
            "accounts": accounts,
            "primaryAccounts": {MAIL_URN: "a"},
        }
    )
    return Batch(default_registry().resolve(session))


def query(sort: list[Any], **arguments: Any) -> dict[str, Any]:
    return {"filter": {"inMailbox": "m1"}, "sort": sort, **arguments}


class TestSortProperties:
    def test_an_advertised_sort_goes_out(self):
        handle = batch().add("Email/query", query([{"property": "receivedAt"}]))
        assert handle.call.arguments["sort"] == [{"property": "receivedAt"}]

    def test_one_the_server_did_not_advertise_is_refused(self):
        sort = [{"property": "subject"}, {"property": "threadSize"}]
        with pytest.raises(CapabilityFieldError, match="emailQuerySortOptions") as excinfo:
            batch().add("Email/query", query(sort))
        assert excinfo.value.requested == ("threadSize",)

    def test_a_server_that_said_nothing_refuses_nothing(self):
        assert batch(sorts=None).add("Email/query", query([{"property": "anything"}]))

    def test_the_account_the_call_names_is_the_one_asked(self):
        # A shared account's server may sort differently from the primary.
        shared = batch(shared_sorts=["receivedAt"])
        assert shared.add("Email/query", query([{"property": "size"}]))
        with pytest.raises(CapabilityFieldError, match="emailQuerySortOptions"):
            shared.add("Email/query", query([{"property": "size"}], accountId="s"))

    def test_query_changes_is_held_to_the_same_list(self):
        sort = [{"property": "threadSize"}]
        with pytest.raises(CapabilityFieldError):
            batch().add("Email/queryChanges", {"sinceQueryState": "q1", "sort": sort})

    def test_a_type_with_no_advertised_list_is_not_checked(self):
        assert batch().add("Mailbox/query", {"sort": [{"property": "anything"}]})

    @pytest.mark.parametrize(
        "sort",
        [[], ["receivedAt"], [{"isAscending": False}], [{"property": 5}], "receivedAt"],
    )
    def test_what_is_not_a_comparator_naming_a_property_is_left_alone(self, sort):
        assert batch().add("Email/query", query(sort))


class TestCollations:
    def test_an_advertised_collation_goes_out(self):
        sort = [{"property": "subject", "collation": "i;unicode-casemap"}]
        assert batch().add("Email/query", query(sort))

    def test_one_the_server_did_not_advertise_is_refused(self):
        sort = [{"property": "subject", "collation": "i;octet"}]
        with pytest.raises(CapabilityFieldError, match="collationAlgorithms"):
            batch().add("Email/query", query(sort))

    def test_it_is_checked_for_every_type(self):
        sort = [{"property": "name", "collation": "i;octet"}]
        with pytest.raises(CapabilityFieldError, match="collationAlgorithms"):
            batch().add("Mailbox/query", {"sort": sort})

    def test_a_server_that_said_nothing_refuses_nothing(self):
        sort = [{"property": "subject", "collation": "i;octet"}]
        assert batch(collations=None).add("Email/query", query(sort))


class TestFileNodes:
    def test_a_file_node_sort_is_held_to_its_own_list(self):
        session = Session.from_wire(
            {
                "capabilities": {URN_CORE: {}, FILENODE_URN: {}},
                "accounts": {
                    "a": {
                        "name": "alice",
                        "accountCapabilities": {
                            FILENODE_URN: {"fileNodeQuerySortOptions": ["name", "size"]}
                        },
                    }
                },
                "primaryAccounts": {FILENODE_URN: "a"},
            }
        )
        files = Batch(default_registry().resolve(session, experimental=True))
        assert files.add("FileNode/query", {"sort": [{"property": "name"}]})
        with pytest.raises(CapabilityFieldError, match="fileNodeQuerySortOptions"):
            files.add("FileNode/query", {"sort": [{"property": "modified"}]})
