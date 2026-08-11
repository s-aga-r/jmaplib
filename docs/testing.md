# Testing your own code

An in-process JMAP server ships with the package, so your tests need no network
and no container. It is the same one this library's own suite runs against.

```python
import httpx
from jmap.auth import BasicAuth
from jmap.client import JMAPClient
from jmap.testing import FakeJMAPServer


def test_unread_count():
    server = FakeJMAPServer(
        capabilities={"urn:ietf:params:jmap:core": {}, "urn:ietf:params:jmap:mail": {}},
        primary_accounts={"urn:ietf:params:jmap:core": "a", "urn:ietf:params:jmap:mail": "a"},
    )
    server.respond(
        "Mailbox/get",
        {
            "accountId": "a",
            "state": "s1",
            "list": [{"id": "m1", "name": "Inbox", "role": "inbox", "unreadEmails": 3}],
        },
    )

    with JMAPClient.connect(
        "https://jmap.example.com/.well-known/jmap",
        auth=BasicAuth("alice@example.com", "pw"),
        http=httpx.Client(**server.client_kwargs()),
    ) as client:
        with client.batch() as batch:
            mailboxes = batch.mail.mailbox.get(ids=None)

    assert mailboxes.result.items[0].unread_emails == 3
```

`client_kwargs()` returns what an `httpx.Client` needs to route to the fake -
it is an `httpx.MockTransport` underneath, so no socket is opened.

## Shaping the session

The constructor is how you describe the server you want to be tested against:

```python
FakeJMAPServer(
    capabilities={...},  # session-level capability objects
    accounts={...},  # account objects, including accountCapabilities
    primary_accounts={...},  # capability URN -> account id
    username="alice@example.com",
    base_url="https://jmap.example.com",
    quirks=ServerQuirks(...),
)
```

This is the real value of the fake: **you can test against a server that does
not support what you need.** Leave a capability out and check your code degrades
properly. Put a limit in and check you chunk. Give an account
`"isReadOnly": True` and check you do not offer to delete anything.

```python
def test_we_cope_without_sieve():
    server = FakeJMAPServer(capabilities={"urn:ietf:params:jmap:core": {}})
    ...  # your code should skip the filtering UI, not crash
```

## Canned responses

```python
server.respond("Email/query", {"accountId": "a", "ids": ["m1", "m2"], "queryState": "q1"})
server.fail("Email/set", "forbidden")
server.handle("Mailbox/get", my_callable)  # full control
```

`respond` answers every call to that method; `fail` makes it a method-level
error; `handle` takes a callable receiving the arguments, for when the response
should depend on the request.

## What the fake actually does

More than replay. It resolves **back-references with this library's own JSON
Pointer evaluator**, so a test of a chained batch exercises the same code the
server would drive:

```python
with client.batch() as batch:
    found = batch.mail.email.query(filter={"inMailbox": "m1"})
    got = batch.mail.email.get(ids=found.ref_ids())
```

That `ref_ids()` really is resolved against the query response. If your chaining
is wrong, the test fails for the same reason production would.

It also stores and concatenates blobs, so an upload-then-reference flow works
end to end.

## Inspecting what was sent

`server.requests` is every request body received, which is how you assert on the
wire rather than on your own abstractions:

```python
with client.batch() as batch:
    batch.mail.email.get(ids=["m1"], properties=["subject"])

sent = server.requests[0]
assert sent["using"] == ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
assert sent["methodCalls"][0][0] == "Email/get"
```

Worth doing for anything where the *shape* matters - that a patch is a patch,
that `using` came out right, that you did not send a default you meant to omit.

## Reproducing bad servers

`ServerQuirks` makes the fake misbehave the way real ones do:

```python
from jmap.testing import ServerQuirks

server = FakeJMAPServer(
    quirks=ServerQuirks(
        unknown_using_is_not_request=True,  # Stalwart: one unknown URN kills the request
        max_calls_in_request=4,  # force the batch planner to split
    )
)
```

`scripted_failures` takes a list of `httpx.Response` objects returned before the
normal handling resumes - which is how you test retry behaviour deterministically
rather than by hoping a real server rate-limits you.

## Async

The same fake serves both clients:

```python
from jmap.aio import AsyncJMAPClient

http = httpx.AsyncClient(**server.client_kwargs())
async with await AsyncJMAPClient.connect(url, auth=auth, http=http) as client:
    ...
```

Worth doing at least once for anything with cancellation or timeout behaviour.
Driving async code from a sync test through a thread portal systematically hides
exactly those bugs, so test the async path natively.

## Checking a real server

`jmap.testing.conformance` reports what a live server actually supports, method
by method:

```console
python -m jmap.testing.conformance https://mail.example.com/.well-known/jmap \
  --user alice@example.com --password ... --markdown
```

Useful when a call works against one server and not another - the matrix says
which methods the server implements rather than which capabilities it claims.
`--experimental` includes the draft specifications.
