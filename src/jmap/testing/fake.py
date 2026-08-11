"""An in-process JMAP server for tests.

Enough of a server to exercise the client end to end without a network: it serves
a Session, accepts requests, resolves back-references *through the library's own
pointer evaluator*, and answers each call from a handler you register.

Reusing :func:`jmap.core.pointer.resolve` rather than reimplementing it is
deliberate. A fake with its own reference resolution tests the fake; a fake that
shares the client's evaluator tests the thing the client will actually do,
including the ``*`` flattening rule that is easy to get wrong in both places at
once.

:class:`ServerQuirks` exists because the interesting bugs are in the gaps between
what a spec says and what a deployment does. Stalwart answers an unknown ``using``
URN with a request-level ``notRequest`` that destroys the whole batch, and puts
some capabilities only in ``accountCapabilities``; both are reproducible here.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from jmap.core.ijson import dumps, loads
from jmap.core.narrow import as_object, is_object
from jmap.core.pointer import PointerError, resolve

#: A handler answers one method call. It receives the resolved arguments and the
#: server, and returns the response arguments.
Handler = Callable[[dict[str, Any], "FakeJMAPServer"], Mapping[str, Any]]

DEFAULT_SESSION_STATE = "session-0"

#: RFC 8620 §6.1's honest default when nothing better is known.
DEFAULT_BLOB_TYPE = "application/octet-stream"

#: RFC 9404 §4.2. What ``Blob/get`` returns when ``properties`` is null.
DEFAULT_BLOB_PROPERTIES = ("data", "size")

#: JMAP spells RFC 3230's algorithm names in lower case, so the mapping to
#: :mod:`hashlib` is explicit rather than a ``lower()`` away.
DIGEST_ALGORITHMS: Mapping[str, str] = {
    "md5": "md5",
    "sha": "sha1",
    "sha-256": "sha256",
    "sha-512": "sha512",
}


@dataclass(slots=True)
class ServerQuirks:
    """Deviations from the spec that real servers exhibit."""

    #: Stalwart: one unadvertised URN in ``using`` fails the entire request with
    #: ``notRequest`` rather than the ``unknownCapability`` the RFC describes.
    unknown_using_is_not_request: bool = False
    #: Reject a request carrying more than this many calls, as a server enforcing
    #: ``maxCallsInRequest`` would.
    max_calls_in_request: int | None = None
    #: Responses to emit before the real one, to drive retry paths.
    scripted_failures: list[httpx.Response] = field(default_factory=lambda: [])


class FakeJMAPServer:
    """A JMAP server backed by :class:`httpx.MockTransport`."""

    base_url: str
    username: str
    session_state: str
    quirks: ServerQuirks

    def __init__(
        self,
        *,
        capabilities: Mapping[str, Any] | None = None,
        accounts: Mapping[str, Any] | None = None,
        primary_accounts: Mapping[str, str] | None = None,
        username: str = "alice@example.com",
        base_url: str = "https://jmap.example.com",
        quirks: ServerQuirks | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        # `is None` rather than `or`: passing {} means "advertise nothing", which
        # is exactly the case worth testing - a server with no primaryAccounts
        # entry must make the client raise rather than guess an account.
        self.capabilities: dict[str, Any] = dict(
            {"urn:ietf:params:jmap:core": {}} if capabilities is None else capabilities
        )
        self.accounts: dict[str, Any] = dict(
            {"a": {"name": username, "isPersonal": True, "isReadOnly": False}}
            if accounts is None
            else accounts
        )
        self.primary_accounts: dict[str, str] = dict(
            {"urn:ietf:params:jmap:core": "a"} if primary_accounts is None else primary_accounts
        )
        self.session_state = DEFAULT_SESSION_STATE
        self.quirks = quirks or ServerQuirks()
        # Blob/upload and Blob/get are implemented rather than stubbed because
        # they *compute*: concatenating sources, slicing ranges and digesting the
        # result is where a client's assumptions get tested, and a canned response
        # would test nothing. Override either with `respond()` as usual.
        self.handlers: dict[str, Handler] = {
            "Core/echo": _echo,
            "Blob/upload": _blob_upload,
            "Blob/get": _blob_get,
        }
        #: Method name -> the `error` invocation arguments to answer with.
        self.errors: dict[str, dict[str, Any]] = {}
        #: Called before normal routing. Return a response to take the request
        #: over, or None to fall through. The seam for driving transport-level
        #: failures without monkeypatching the router.
        self.intercept: Callable[[httpx.Request], httpx.Response | None] | None = None
        #: Every request body received, for assertions about what went on the wire.
        self.requests: list[dict[str, Any]] = []
        self.created_ids: dict[str, str] = {}
        #: Blob id -> (bytes, content type), populated by uploads.
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self._blob_counter = 0
        #: Raw ``text/event-stream`` chunks the next event-source connection gets.
        self.push_events: list[str] = []
        #: Headers of every event-source request, so a test can assert that
        #: ``Last-Event-ID`` really went back on a reconnect.
        self.event_source_requests: list[dict[str, str]] = []

    # -- configuration ------------------------------------------------------ #
    def handle(self, method: str, handler: Handler) -> None:
        """Register the answer for ``method``."""
        self.handlers[method] = handler

    def respond(self, method: str, arguments: Mapping[str, Any]) -> None:
        """Register a fixed response for ``method``."""
        self.handlers[method] = lambda _args, _server: arguments

    def fail(self, method: str, error_type: str, **extra: Any) -> None:
        """Make ``method`` answer with a method error (RFC 8620 §3.6.2).

        A failed call is *renamed* to ``error`` rather than annotated, which is
        what the client's demultiplexer keys off, so the fake has to reproduce
        that shape rather than return an error-looking payload.
        """
        self.errors[method] = {"type": error_type, **extra}

    @property
    def session_document(self) -> dict[str, Any]:
        return {
            "capabilities": self.capabilities,
            "accounts": self.accounts,
            "primaryAccounts": self.primary_accounts,
            "username": self.username,
            "apiUrl": f"{self.base_url}/jmap/",
            "downloadUrl": f"{self.base_url}/jmap/download/"
            "{accountId}/{blobId}/{name}?accept={type}",
            "uploadUrl": f"{self.base_url}/jmap/upload/{{accountId}}/",
            "eventSourceUrl": f"{self.base_url}/jmap/eventsource/"
            "?types={types}&closeafter={closeafter}&ping={ping}",
            "state": self.session_state,
        }

    # -- transport ---------------------------------------------------------- #
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.route)

    def client_kwargs(self) -> dict[str, Any]:
        """Arguments for an ``httpx.Client`` wired to this server."""
        return {"transport": self.transport(), "follow_redirects": True}

    def route(self, request: httpx.Request) -> httpx.Response:
        """Answer one request the way a JMAP server would."""
        if self.intercept is not None:
            taken = self.intercept(request)
            if taken is not None:
                return taken
        path = request.url.path
        if path == "/.well-known/jmap":
            # 307 like Stalwart. Tests must assert the redirect was followed and
            # the Authorization header survived, never the numeric code.
            return httpx.Response(307, headers={"Location": f"{self.base_url}/jmap/session"})
        if path.endswith("/jmap/session"):
            return httpx.Response(200, json=self.session_document)
        if "/jmap/eventsource/" in path:
            return self._event_source(request)
        if "/jmap/upload/" in path:
            return self._upload(request)
        if "/jmap/download/" in path:
            return self._download(path)
        if path.rstrip("/").endswith("/jmap"):
            return self._api(request)
        return httpx.Response(404, json={"type": "about:blank", "status": 404})

    def push(
        self,
        account_id: str,
        states: Mapping[str, str],
        *,
        event_id: str = "",
        retry: int | None = None,
    ) -> None:
        """Queue a ``state`` event for the next event-source connection.

        Queued rather than delivered, because the fake has no way to interrupt a
        client that is not currently connected - and a client that reconnects
        expects to be told what it missed, which is exactly what the queue models.
        """
        self.push_events.append(
            _state_event(
                {"@type": "StateChange", "changed": {account_id: dict(states)}}, event_id, retry
            )
        )

    def push_ping(self, interval: int) -> None:
        """Queue a ``ping`` event.

        Deliberately carries no id: RFC 8620 §7.3 forbids it, and a fake that sent
        one would hide the bug where a client resumes from a keep-alive.
        """
        self.push_events.append(f'event: ping\ndata: {{"interval": {interval}}}\n\n')

    def _event_source(self, request: httpx.Request) -> httpx.Response:
        """RFC 8620 §7.3. A ``text/event-stream`` of whatever has been queued."""
        self.event_source_requests.append(dict(request.headers))
        events, self.push_events = self.push_events, []
        body = "".join(events)
        return httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
        )

    def store_blob(self, content: bytes, content_type: str = DEFAULT_BLOB_TYPE) -> str:
        """Add a blob and return its id, as either upload path would."""
        self._blob_counter += 1
        blob_id = f"B{self._blob_counter}"
        self.blobs[blob_id] = (content, content_type)
        return blob_id

    def concatenate(self, sources: Sequence[Any]) -> bytes:
        """Resolve one ``Blob/upload`` ``data`` array into octets (RFC 9404 §4.1).

        A ``blobId`` may be a ``#creationId`` naming a blob created earlier in the
        same request, which is the mechanism that makes server-side splicing
        possible - so it is resolved against ``created_ids`` here rather than
        treated as a literal id.

        Raises :class:`ValueError` for anything the RFC requires the server to
        refuse: an unresolvable id, a range past the end, or a source that names
        no data at all.
        """
        out = bytearray()
        for source in sources:
            if not isinstance(source, Mapping):
                raise ValueError("a data source must be an object")
            entry = cast("Mapping[str, Any]", source)
            if "data:asText" in entry:
                out += str(entry["data:asText"]).encode()
            elif "data:asBase64" in entry:
                out += base64.b64decode(str(entry["data:asBase64"]), validate=True)
            elif "blobId" in entry:
                out += self._slice(entry)
            else:
                raise ValueError("a data source must name exactly one octet source")
        return bytes(out)

    def resolve_blob_id(self, reference: str) -> str:
        """Turn a ``#creationId`` into the blob it named, or pass an id through.

        A ``#`` prefix inside an argument *value* is a creation reference resolved
        against ``createdIds`` (RFC 8620 §5.3), which is a different mechanism from
        the ``#argument`` result references handled in :meth:`_resolve_references`.
        Blobs lean on it, because RFC 9404 §4.1 has every upload populate
        ``createdIds`` whether the client asked for one or not.
        """
        if not reference.startswith("#"):
            return reference
        return self.created_ids.get(reference[1:], "")

    def _slice(self, source: Mapping[str, Any]) -> bytes:
        reference = str(source["blobId"])
        found = self.blobs.get(self.resolve_blob_id(reference))
        if found is None:
            raise ValueError(f"no such blob {reference!r}")
        data = found[0]
        offset = int(source.get("offset") or 0)
        length = source.get("length")
        end = len(data) if length is None else offset + int(length)
        # RFC 9404 §4.1: a range that begins or extends past the end makes the
        # whole creation invalid, rather than being silently clamped.
        if offset > len(data) or end > len(data):
            raise ValueError(f"range {offset}:{end} is past the end of blob {reference!r}")
        return data[offset:end]

    def _upload(self, request: httpx.Request) -> httpx.Response:
        """RFC 8620 §6.1. Blobs move over plain HTTP, not as method calls."""
        content_type = request.headers.get("Content-Type", DEFAULT_BLOB_TYPE)
        blob_id = self.store_blob(request.content, content_type)
        account_id = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(
            201,
            json={
                "accountId": account_id,
                "blobId": blob_id,
                "type": content_type,
                "size": len(request.content),
            },
        )

    def _download(self, path: str) -> httpx.Response:
        # downloadUrl is {accountId}/{blobId}/{name}, so the id is the middle part.
        parts = path.split("/jmap/download/", 1)[1].split("/")
        blob_id = parts[1] if len(parts) > 1 else ""
        found = self.blobs.get(blob_id)
        if found is None:
            return httpx.Response(404, json={"type": "about:blank", "status": 404})
        content, content_type = found
        return httpx.Response(200, content=content, headers={"Content-Type": content_type})

    def _api(self, request: httpx.Request) -> httpx.Response:
        if self.quirks.scripted_failures:
            return self.quirks.scripted_failures.pop(0)

        raw = loads(request.content)
        if not isinstance(raw, dict):
            return _problem("urn:ietf:params:jmap:error:notRequest", 400)
        body = cast("dict[str, Any]", raw)
        self.requests.append(body)

        using: set[str] = set(body.get("using") or ())
        unknown = using - set(self.capabilities) - self._account_capability_urns()
        if unknown:
            problem_type = (
                "urn:ietf:params:jmap:error:notRequest"
                if self.quirks.unknown_using_is_not_request
                else "urn:ietf:params:jmap:error:unknownCapability"
            )
            return _problem(problem_type, 400)

        calls: list[list[Any]] = body.get("methodCalls") or []
        if (
            self.quirks.max_calls_in_request is not None
            and len(calls) > self.quirks.max_calls_in_request
        ):
            return _problem("urn:ietf:params:jmap:error:limit", 400, limit="maxCallsInRequest")

        responses: list[list[Any]] = []
        answered: dict[str, dict[str, Any]] = {}
        for call in calls:
            name, arguments, call_id = str(call[0]), call[1], str(call[2])
            try:
                resolved = self._resolve_references(arguments, answered)
            except PointerError:
                responses.append(["error", {"type": "invalidResultReference"}, call_id])
                continue
            failure = self.errors.get(name)
            if failure is not None:
                responses.append(["error", dict(failure), call_id])
                continue
            handler = self.handlers.get(name)
            if handler is None:
                responses.append(["error", {"type": "unknownMethod"}, call_id])
                continue
            result = dict(handler(resolved, self))
            answered[call_id] = result
            responses.append([name, result, call_id])

        return httpx.Response(
            200,
            content=dumps(
                {
                    "methodResponses": responses,
                    "sessionState": self.session_state,
                    **({"createdIds": self.created_ids} if self.created_ids else {}),
                }
            ),
            headers={"Content-Type": "application/json"},
        )

    def _account_capability_urns(self) -> set[str]:
        urns: set[str] = set()
        for account in self.accounts.values():
            urns |= set(account.get("accountCapabilities") or {})
        return urns

    def _resolve_references(
        self, arguments: Mapping[str, Any], answered: Mapping[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace ``#name`` arguments using the library's own pointer evaluator."""
        resolved: dict[str, Any] = {}
        for key, value in arguments.items():
            if not key.startswith("#"):
                resolved[key] = value
                continue
            reference = value
            source = answered.get(str(reference.get("resultOf")))
            if source is None:
                raise PointerError(str(reference.get("path")), "no such earlier call")
            resolved[key[1:]] = resolve(source, str(reference.get("path")))
        return resolved


def _state_event(payload: Mapping[str, Any], event_id: str, retry: int | None = None) -> str:
    """One ``state`` event in ``text/event-stream`` framing."""
    lines = [f"retry: {retry}"] if retry is not None else []
    if event_id:
        lines.append(f"id: {event_id}")
    lines += ["event: state", f"data: {dumps(payload)}", ""]
    return "\n".join(lines) + "\n"


def _echo(arguments: dict[str, Any], _server: FakeJMAPServer) -> dict[str, Any]:
    return arguments


def _blob_upload(arguments: dict[str, Any], server: FakeJMAPServer) -> dict[str, Any]:
    """``Blob/upload`` (RFC 9404 §4.1).

    Each created blobId goes into ``createdIds`` whether or not the client asked
    for one, which the RFC requires: it is what lets a later call in the same
    request reference the blob by ``#creationId``.
    """
    created: dict[str, Any] = {}
    not_created: dict[str, Any] = {}
    creations = cast("Mapping[str, Any]", arguments.get("create") or {})
    for creation_id, upload in creations.items():
        # A real server answers a malformed creation with notCreated rather than
        # falling over, and a fake that crashes instead teaches nothing.
        if not is_object(upload):
            not_created[creation_id] = {
                "type": "invalidProperties",
                "description": "an UploadObject must be an object",
            }
            continue
        try:
            content = server.concatenate(as_object(upload).get("data") or [])
        except ValueError as exc:
            # The RFC forbids guessing: an unusable source fails the creation.
            not_created[creation_id] = {"type": "invalidProperties", "description": str(exc)}
            continue
        content_type = upload.get("type") or DEFAULT_BLOB_TYPE
        blob_id = server.store_blob(content, content_type)
        server.created_ids[creation_id] = blob_id
        created[creation_id] = {"id": blob_id, "type": content_type, "size": len(content)}
    return {
        "accountId": arguments.get("accountId"),
        "created": created,
        "notCreated": not_created,
    }


def _blob_get(arguments: dict[str, Any], server: FakeJMAPServer) -> dict[str, Any]:
    """``Blob/get`` (RFC 9404 §4.2), including the range and encoding rules."""
    properties = arguments.get("properties") or list(DEFAULT_BLOB_PROPERTIES)
    offset = int(arguments.get("offset") or 0)
    raw_length = arguments.get("length")
    length = None if raw_length is None else int(raw_length)

    items: list[dict[str, Any]] = []
    not_found: list[str] = []
    for identifier in cast("Sequence[Any]", arguments.get("ids") or []):
        # `["#cat"]` is how RFC 9404 §4.1.2 reads a blob created earlier in the
        # same request: a creation reference in the value, not a result reference.
        blob_id = server.resolve_blob_id(str(identifier))
        found = server.blobs.get(blob_id)
        if found is None:
            not_found.append(str(identifier))
            continue
        items.append(_blob_properties(blob_id, found[0], properties, offset, length))
    return {"accountId": arguments.get("accountId"), "list": items, "notFound": not_found}


def _blob_properties(
    blob_id: str, data: bytes, properties: Sequence[Any], offset: int, length: int | None
) -> dict[str, Any]:
    end = len(data) if length is None else offset + length
    selected = data[offset:end]
    # A null length is "the rest of the blob", so it can only truncate by starting
    # past the end; an explicit length truncates whenever it overshoots.
    truncated = offset > len(data) if length is None else end > len(data)

    try:
        text: str | None = selected.decode()
    except UnicodeDecodeError:
        text = None
    encoded = base64.b64encode(selected).decode("ascii")

    item: dict[str, Any] = {"id": blob_id}
    for name in map(str, properties):
        if name == "size":
            # Always the whole blob, never the selected range.
            item["size"] = len(data)
        elif name == "data":
            # The adaptive form: text when it decodes, base64 when it does not.
            if text is None:
                item["data:asBase64"] = encoded
                item["isEncodingProblem"] = True
            else:
                item["data:asText"] = text
        elif name == "data:asText":
            if text is None:
                item["isEncodingProblem"] = True
            else:
                item["data:asText"] = text
        elif name == "data:asBase64":
            item["data:asBase64"] = encoded
        elif name.startswith("digest:"):
            digest = _digest(name.removeprefix("digest:"), selected)
            if digest is not None:
                item[name] = digest
    if truncated:
        item["isTruncated"] = True
    return item


def _digest(algorithm: str, data: bytes) -> str | None:
    """Base64 of ``data``'s digest, or ``None`` for an algorithm we do not offer.

    Names are the lowercased RFC 3230 registry spellings, which is how JMAP writes
    them - ``sha-256``, not ``SHA-256``.
    """
    name = DIGEST_ALGORITHMS.get(algorithm)
    if name is None:
        return None
    return base64.b64encode(hashlib.new(name, data).digest()).decode("ascii")


def _problem(problem_type: str, status: int, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"type": problem_type, "status": status, **extra}
    return httpx.Response(
        status, content=dumps(body), headers={"Content-Type": "application/problem+json"}
    )
