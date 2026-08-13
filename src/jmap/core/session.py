"""The Session resource (RFC 8620 §2) - the client's whole view of the server.

Everything capability-driven starts here: which URNs exist, which account is the
default for each of them, what the server's limits are, and where the API, upload,
download and event-source endpoints live.

Two things are easy to get wrong and are handled explicitly:

**URLs may be relative.** RFC 8620 does not require absolute URLs, and Stalwart
emits relative ones (``/jmap/``) when no public URL is configured. They must be
resolved against the URL the session was *actually fetched from* - which, because
discovery usually starts at ``/.well-known/jmap`` and follows a redirect, is the
post-redirect URL, not the one the caller typed.

**Capabilities are per-account, and the two maps are not nested.** The session's
top-level ``capabilities`` and an account's ``accountCapabilities`` are unioned,
never treated as subset and superset. Stalwart advertises
``urn:ietf:params:jmap:mail:share`` and ``urn:stalwart:jmap`` *only* in
``accountCapabilities``, so a client that asserts containment sees the wrong
answer on a real server.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Self, cast
from urllib.parse import urljoin, urlsplit

from jmap.core.errors import JMAPError
from jmap.core.ids import Id
from jmap.core.limits import Limits

CORE_URN: Final = "urn:ietf:params:jmap:core"

#: Hosts whose cleartext endpoints leak nowhere off the machine.
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})


class InsecureEndpointError(JMAPError):
    """A session document pointed an endpoint at a weaker channel.

    The endpoint URLs are the one part of the session a client *acts* on with
    credentials attached: every subsequent request carries the Authorization
    header to wherever ``apiUrl`` and friends say. A document fetched over
    https that names an ``http://`` endpoint is therefore not a config quirk,
    it is a downgrade - the classic shape of a MITM harvesting Basic
    credentials in cleartext. Cross-host https endpoints stay legal; real
    providers serve upload and download from separate hosts.
    """

    def __init__(self, field: str, url: str, *, base_url: str) -> None:
        self.field = field
        self.url = url
        super().__init__(
            f"the session document ({base_url}) names {url!r} as its {field}; refusing "
            f"to send credentials over a weaker scheme than the session itself came over"
        )


def _malformed(field: str, value: Any) -> ValueError:
    return ValueError(
        f"the session document's {field!r} is not the shape RFC 8620 requires: "
        f"got {type(value).__name__}"
    )


def _check_endpoint_scheme(field: str, url: str, base_scheme: str, *, base_url: str) -> None:
    """Refuse an endpoint on a weaker scheme than the session's own channel.

    Only enforced when the fetch context is known (``base_url`` given): parsing
    a cached document offline is the caller's own input, and a session fetched
    over http has already accepted cleartext, so its endpoints may stay http -
    which is what keeps an internal-network deployment working. Loopback is
    always allowed; ``wss``/``ws`` follow the same rule for the WebSocket URL a
    capability may carry a client through here.
    """
    split = urlsplit(url)
    scheme = split.scheme
    if scheme in ("https", "wss") or not scheme:
        return
    downgraded = base_scheme == "https" and scheme in ("http", "ws")
    foreign = scheme not in ("http", "ws")
    if foreign or (downgraded and (split.hostname or "").lower() not in _LOOPBACK_HOSTS):
        raise InsecureEndpointError(field, url, base_url=base_url)


class Account:
    """One account the authenticated principal can access (RFC 8620 §2)."""

    __slots__ = ("account_capabilities", "id", "is_personal", "is_read_only", "name")

    id: Id
    name: str
    is_personal: bool
    is_read_only: bool
    account_capabilities: dict[str, Any]

    def __init__(
        self,
        id: Id,
        name: str,
        *,
        is_personal: bool,
        is_read_only: bool,
        account_capabilities: Mapping[str, Any],
    ) -> None:
        self.id = id
        self.name = name
        self.is_personal = is_personal
        self.is_read_only = is_read_only
        self.account_capabilities = dict(account_capabilities)

    @classmethod
    def from_wire(cls, account_id: str, data: Mapping[str, Any]) -> Self:
        return cls(
            Id(account_id),
            str(data.get("name", "")),
            is_personal=bool(data.get("isPersonal", False)),
            is_read_only=bool(data.get("isReadOnly", False)),
            account_capabilities=data.get("accountCapabilities") or {},
        )

    def __repr__(self) -> str:
        return f"Account({self.id!r}, {self.name!r}, read_only={self.is_read_only})"


class Session:
    """A parsed Session resource."""

    __slots__ = (
        "accounts",
        "api_url",
        "capabilities",
        "download_url",
        "event_source_url",
        "primary_accounts",
        "raw",
        "state",
        "upload_url",
        "username",
    )

    capabilities: dict[str, Any]
    accounts: dict[Id, Account]
    primary_accounts: dict[str, Id]
    username: str
    api_url: str
    download_url: str
    upload_url: str
    event_source_url: str
    state: str
    raw: dict[str, Any]

    def __init__(
        self,
        *,
        capabilities: Mapping[str, Any],
        accounts: Mapping[Id, Account],
        primary_accounts: Mapping[str, Id],
        username: str,
        api_url: str,
        download_url: str,
        upload_url: str,
        event_source_url: str,
        state: str,
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        self.capabilities = dict(capabilities)
        self.accounts = dict(accounts)
        self.primary_accounts = dict(primary_accounts)
        self.username = username
        self.api_url = api_url
        self.download_url = download_url
        self.upload_url = upload_url
        self.event_source_url = event_source_url
        self.state = state
        self.raw = dict(raw or {})

    @classmethod
    def from_wire(cls, data: Mapping[str, Any], *, base_url: str = "") -> Self:
        """Parse a session document.

        ``base_url`` must be the URL the document was fetched from *after* any
        redirects, so relative endpoint URLs resolve correctly.
        """

        base_scheme = urlsplit(base_url).scheme if base_url else ""

        def absolute(field: str, value: Any) -> str:
            text = str(value or "")
            resolved = urljoin(base_url, text) if base_url else text
            if base_scheme and resolved:
                _check_endpoint_scheme(field, resolved, base_scheme, base_url=base_url)
            return resolved

        # This is the entry point for a document fetched from the network, so
        # the shapes are checked rather than cast: a hostile or broken server
        # must produce a ValueError a caller can catch, not an AttributeError
        # from deep inside a comprehension.
        raw_accounts = data.get("accounts") or {}
        if not isinstance(raw_accounts, Mapping):
            raise _malformed("accounts", raw_accounts)
        raw_primary = data.get("primaryAccounts") or {}
        if not isinstance(raw_primary, Mapping):
            raise _malformed("primaryAccounts", raw_primary)
        capabilities = data.get("capabilities") or {}
        if not isinstance(capabilities, Mapping):
            raise _malformed("capabilities", capabilities)

        accounts: dict[Id, Account] = {}
        for account_id, account in cast("Mapping[str, Any]", raw_accounts).items():
            if not isinstance(account, Mapping):
                raise _malformed(f"accounts[{account_id!r}]", account)
            accounts[Id(str(account_id))] = Account.from_wire(str(account_id), account)

        primary: dict[str, Id] = {}
        for urn, account_id in cast("Mapping[str, Any]", raw_primary).items():
            if not isinstance(account_id, str):
                raise _malformed(f"primaryAccounts[{urn!r}]", account_id)
            primary[str(urn)] = Id(account_id)
        return cls(
            capabilities=capabilities,
            accounts=accounts,
            primary_accounts=primary,
            username=str(data.get("username", "")),
            api_url=absolute("apiUrl", data.get("apiUrl")),
            download_url=absolute("downloadUrl", data.get("downloadUrl")),
            upload_url=absolute("uploadUrl", data.get("uploadUrl")),
            event_source_url=absolute("eventSourceUrl", data.get("eventSourceUrl")),
            state=str(data.get("state", "")),
            raw=data,
        )

    # -- capability resolution --------------------------------------------- #
    @property
    def limits(self) -> Limits:
        """Server limits from the core capability."""
        return Limits.from_capability(self.capabilities.get(CORE_URN) or {})

    def advertised_for(self, account_id: Id | None = None) -> frozenset[str]:
        """URNs usable against ``account_id``: the union of the session-level and
        account-level maps.

        A union, never a containment check - see the module docstring.
        """
        urns = set(self.capabilities)
        if account_id is not None:
            account = self.accounts.get(account_id)
            if account is not None:
                urns |= set(account.account_capabilities)
        return frozenset(urns)

    def capability_value(self, urn: str, account_id: Id | None = None) -> Mapping[str, Any]:
        """The capability object for ``urn``.

        The account-level value wins where both exist: RFC 8620 §2 says
        ``accountCapabilities`` carries the values that apply to that account.
        """
        if account_id is not None:
            account = self.accounts.get(account_id)
            if account is not None and urn in account.account_capabilities:
                scoped: Any = account.account_capabilities[urn]
                if isinstance(scoped, Mapping):
                    return cast("Mapping[str, Any]", scoped)
        value: Any = self.capabilities.get(urn)
        # A non-mapping value is out of spec but survivable: treat it as absent
        # rather than handing the caller something it cannot index.
        return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}

    def capability_account(self, urn: str, account_id: Id | None = None) -> Id | None:
        """Which account's copy of ``urn``'s capability object to read.

        Asking without naming an account is the common case and used to mean
        "the session-level one", which on a real server is very nearly useless:
        Stalwart leaves twelve of its sixteen capability objects **empty** at
        session level and puts every actual limit in ``accountCapabilities``.
        ``maxDelayedSend``, ``emailQuerySortOptions``, ``forbiddenNameChars``,
        ``supportedDigestAlgorithms`` - all of them - read as absent, so the
        gates built on them stop gating. ``supportedDigestAlgorithms`` is the
        worst of the set, because empty does not read as "unknown, allow it" but
        as "supports nothing", and a perfectly legal ``digest:sha-256`` is then
        refused before it ever reaches the wire.

        So an unnamed account resolves to the one the server itself nominates for
        this capability, and only then to whatever the session implies. Both are
        the server's own statements, not inference on our part.
        """
        if account_id is not None:
            return account_id
        return self.primary_account_for(urn) or self.implied_account()

    def account_capability_value(self, urn: str, account_id: Id | None) -> Mapping[str, Any]:
        """The capability object for ``urn`` from ``accountCapabilities`` *only*.

        Distinct from :meth:`capability_value`, which falls back to the
        session-level map. Some capabilities are defined to appear only per
        account - RFC 9670's ``:principals:owner`` is the case - and for those the
        fallback would read a session-level value that a conformant server never
        publishes, turning "this account has no owner" into a confident wrong
        answer against a server that publishes one anyway.
        """
        if account_id is None:
            return {}
        account = self.accounts.get(account_id)
        if account is None:
            return {}
        value: Any = account.account_capabilities.get(urn)
        return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}

    def primary_account_for(self, urn: str) -> Id | None:
        """The default account for a capability, per ``primaryAccounts``.

        Returns ``None`` rather than guessing. A caller that picks "the first
        account" instead will eventually send ``Email/*`` at a calendar-only
        shared account and get ``accountNotSupportedByMethod``.
        """
        return self.primary_accounts.get(urn)

    def implied_account(self) -> Id | None:
        """The account this session implies when the caller names none.

        Blob upload and download are account-scoped but belong to no capability,
        so ``primaryAccounts`` has no entry to look up: RFC 8620 §2 keys that map
        by capability URN, and the core capability has no data to be primary
        *for*. Real servers list nothing there for it. That leaves "whose blobs?"
        with no direct answer, and refusing to answer it at all broke every
        upload against servers where nothing was ambiguous in the first place.

        Two situations are unambiguous, and both are checked here:

        * **Every ``primaryAccounts`` entry naming the same account.** This is
          not a guess. It is the server stating, for every kind of data it holds,
          that this is the user's main account - so a personal account alongside
          a shared team one resolves cleanly, because nothing points at the
          shared one. A session with mail in one account and calendars in another
          is *not* unanimous and gets no answer.
        * **A session with exactly one account.** Nothing to choose between. This
          is the case a freshly provisioned server presents, where
          ``primaryAccounts`` may be empty entirely.

        Anything else is ``None``, because picking a favourite among several is
        how ``Email/*`` ends up aimed at a calendar-only shared account - which
        is what :meth:`primary_account_for` refuses to do, for the same reason.
        """
        primaries = set(self.primary_accounts.values())
        if len(primaries) == 1:
            unanimous = next(iter(primaries))
            # A server naming an account it did not also list is malformed; the
            # id would be unusable, so fall through rather than pass it on.
            if unanimous in self.accounts:
                return unanimous
        if len(self.accounts) == 1:
            return next(iter(self.accounts))
        return None

    def accounts_with(self, urn: str) -> tuple[Id, ...]:
        """Every account whose ``accountCapabilities`` includes ``urn``."""
        return tuple(
            account_id
            for account_id, account in self.accounts.items()
            if urn in account.account_capabilities
        )

    def is_read_only(self, account_id: Id) -> bool:
        account = self.accounts.get(account_id)
        return account.is_read_only if account is not None else False

    def __repr__(self) -> str:
        return (
            f"Session(username={self.username!r}, accounts={len(self.accounts)}, "
            f"capabilities={len(self.capabilities)}, state={self.state!r})"
        )
