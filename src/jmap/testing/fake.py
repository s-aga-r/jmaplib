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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from jmap.core.ijson import dumps, loads
from jmap.core.pointer import PointerError, resolve

#: A handler answers one method call. It receives the resolved arguments and the
#: server, and returns the response arguments.
Handler = Callable[[dict[str, Any], "FakeJMAPServer"], Mapping[str, Any]]

DEFAULT_SESSION_STATE = "session-0"


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
        self.handlers: dict[str, Handler] = {"Core/echo": _echo}
        #: Called before normal routing. Return a response to take the request
        #: over, or None to fall through. The seam for driving transport-level
        #: failures without monkeypatching the router.
        self.intercept: Callable[[httpx.Request], httpx.Response | None] | None = None
        #: Every request body received, for assertions about what went on the wire.
        self.requests: list[dict[str, Any]] = []
        self.created_ids: dict[str, str] = {}

    # -- configuration ------------------------------------------------------ #
    def handle(self, method: str, handler: Handler) -> None:
        """Register the answer for ``method``."""
        self.handlers[method] = handler

    def respond(self, method: str, arguments: Mapping[str, Any]) -> None:
        """Register a fixed response for ``method``."""
        self.handlers[method] = lambda _args, _server: arguments

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
        if path.rstrip("/").endswith("/jmap"):
            return self._api(request)
        return httpx.Response(404, json={"type": "about:blank", "status": 404})

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


def _echo(arguments: dict[str, Any], _server: FakeJMAPServer) -> dict[str, Any]:
    return arguments


def _problem(problem_type: str, status: int, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"type": problem_type, "status": status, **extra}
    return httpx.Response(
        status, content=dumps(body), headers={"Content-Type": "application/problem+json"}
    )
