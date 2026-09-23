"""The benchmark cases themselves.

Each case is a zero-argument callable registered under a name. Setup that is not
the thing being measured happens once, at registration time, so the timed body
contains only the work under test.

Cases are grouped by layer, because the layers have very different profiles: the
kernel is pure functions over plain dicts and should be fast in absolute terms,
the model layer is dominated by pydantic, and the end-to-end cases exist to
confirm the first two actually add up to the whole.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from benchmarks import payloads
from jmap.auth import BasicAuth
from jmap.client import JMAPClient
from jmap.core import ijson
from jmap.core.patch import PatchBuilder
from jmap.core.pointer import resolve
from jmap.core.response import Response
from jmap.core.session import Session
from jmap.defaults import default_registry
from jmap.models.mail.objects import Email, Mailbox
from jmap.models.responses import GetResponse
from jmap.push.sse import SSEParser
from jmap.testing import FakeJMAPServer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

Case = "Callable[[], object]"

#: name -> (group, callable)
REGISTRY: dict[str, tuple[str, Callable[[], object]]] = {}


def case(name: str, group: str) -> Callable[[Callable[[], object]], Callable[[], object]]:
    def register(func: Callable[[], object]) -> Callable[[], object]:
        REGISTRY[name] = (group, func)
        return func

    return register


# --------------------------------------------------------------------------- #
# Shared, built once - constructing these is not what we are measuring.
# --------------------------------------------------------------------------- #
SESSION_WIRE = payloads.session()
RESPONSE_WIRE = payloads.full_response(100)
EMAILS_WIRE = payloads.email_get_response(100)
MAILBOXES_WIRE = payloads.mailbox_get_response(40)
RESPONSE_TEXT = json.dumps(RESPONSE_WIRE)
RESPONSE_BYTES = RESPONSE_TEXT.encode()
QUERY_RESPONSE = RESPONSE_WIRE["methodResponses"][0][1]

REQUEST_WIRE: dict[str, Any] = {
    "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
    "methodCalls": [
        ["Email/query", {"accountId": "c1", "filter": {"inMailbox": "mb-inbox"}, "limit": 50}, "0"],
        [
            "Email/get",
            {
                "accountId": "c1",
                "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
            },
            "1",
        ],
        [
            "Email/set",
            {
                "accountId": "c1",
                "update": {f"M{n:08x}": {"keywords/$seen": True} for n in range(50)},
            },
            "2",
        ],
    ],
}

SESSION = Session.from_wire(SESSION_WIRE)
REGISTRY_DEFAULT = default_registry()
ACTIVE = REGISTRY_DEFAULT.resolve(SESSION, payloads.ACCOUNT_ID)


# --------------------------------------------------------------------------- #
# Kernel: pure functions over decoded JSON
# --------------------------------------------------------------------------- #
@case("ijson.loads/response-100", "kernel")
def ijson_loads() -> object:
    return ijson.loads(RESPONSE_BYTES)


@case("json.loads/response-100 (floor)", "kernel")
def stdlib_loads() -> object:
    """The stdlib cost of the same parse - the floor `ijson.loads` is measured against."""
    return json.loads(RESPONSE_BYTES)


@case("ijson.dumps/request-50", "kernel")
def ijson_dumps() -> object:
    return ijson.dumps(REQUEST_WIRE)


@case("json.dumps/request-50 (floor)", "kernel")
def stdlib_dumps() -> object:
    return json.dumps(REQUEST_WIRE, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@case("pointer.resolve/ids", "kernel")
def pointer_ids() -> object:
    return resolve(QUERY_RESPONSE, "/ids")


@case("pointer.resolve/wildcard", "kernel")
def pointer_wildcard() -> object:
    return resolve(EMAILS_WIRE, "/list/*/id")


@case("Session.from_wire", "kernel")
def session_parse() -> object:
    return Session.from_wire(SESSION_WIRE)


@case("Response.from_wire/100", "kernel")
def response_parse() -> object:
    return Response.from_wire(RESPONSE_WIRE)


@case("PatchBuilder/50-keywords", "kernel")
def patch_build() -> object:
    builder = PatchBuilder()
    for index in range(50):
        builder.set(f"keywords/kw{index}", True)
    return builder.build()


#: 256 KiB of short event-stream lines ending in LF alone, which is what real
#: servers send. It is the input where looking for a CR the naive way searches
#: to the end of the buffer once per line.
SSE_LF_CHUNK = ("data: x\n" * 32_768).encode()


@case("sse.feed/256KiB-lf-lines", "kernel")
def sse_lf_lines() -> object:
    return list(SSEParser().feed_bytes(SSE_LF_CHUNK))


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #
@case("registry.resolve", "capabilities")
def capability_resolve() -> object:
    return REGISTRY_DEFAULT.resolve(SESSION, payloads.ACCOUNT_ID)


@case("active.using_for/3-calls", "capabilities")
def using_derivation() -> object:
    return ACTIVE.using_for(["Email/query", "Email/get", "Email/set"])


@case("session.capability_value", "capabilities")
def capability_value() -> object:
    return SESSION.capability_value(payloads.MAIL, payloads.ACCOUNT_ID)


# --------------------------------------------------------------------------- #
# Models: pydantic validation, the layer that dominates a real /get
# --------------------------------------------------------------------------- #
@case("Email.from_wire x100", "models")
def email_models() -> object:
    return [Email.from_wire(item) for item in EMAILS_WIRE["list"]]


@case("Mailbox.from_wire x40", "models")
def mailbox_models() -> object:
    return [Mailbox.from_wire(item) for item in MAILBOXES_WIRE["list"]]


@case("GetResponse[Email]/100", "models")
def get_response_parse() -> object:
    return GetResponse[Email].model_validate(EMAILS_WIRE)


@case("Email.to_wire x100", "models")
def email_serialise() -> object:
    return [model.to_wire() for model in _EMAIL_MODELS]


_EMAIL_MODELS = [Email.from_wire(item) for item in EMAILS_WIRE["list"]]


# --------------------------------------------------------------------------- #
# End to end: the whole client path over an in-process server
# --------------------------------------------------------------------------- #
def _fake_client() -> tuple[FakeJMAPServer, JMAPClient]:
    server = FakeJMAPServer(
        capabilities=SESSION_WIRE["capabilities"],
        accounts=SESSION_WIRE["accounts"],
        primary_accounts=SESSION_WIRE["primaryAccounts"],
    )
    server.respond("Email/query", QUERY_RESPONSE)
    server.respond("Email/get", EMAILS_WIRE)
    server.respond("Mailbox/get", MAILBOXES_WIRE)
    client = JMAPClient.connect(
        "https://jmap.example.com/.well-known/jmap",
        auth=BasicAuth("user1@example.com", "pw"),
        http=httpx.Client(**server.client_kwargs()),
    )
    return server, client


_SERVER, _CLIENT = _fake_client()


@case("e2e query->get chain", "end-to-end")
def e2e_chain() -> object:
    with _CLIENT.batch() as batch:
        query = batch.mail.email.query(filter={"inMailbox": "mb-inbox"}, limit=100)
        emails = batch.mail.email.get(ids=query.ref_ids())
    return emails.result.items


@case("e2e mailbox get", "end-to-end")
def e2e_mailboxes() -> object:
    with _CLIENT.batch() as batch:
        mailboxes = batch.mail.mailbox.get(ids=None)
    return mailboxes.result.items


def groups() -> Iterator[str]:
    seen: list[str] = []
    for group, _ in REGISTRY.values():
        if group not in seen:
            seen.append(group)
    yield from seen
