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
from urllib.parse import urljoin

from jmap.core.ids import Id
from jmap.core.limits import Limits

CORE_URN: Final = "urn:ietf:params:jmap:core"


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

        def absolute(value: Any) -> str:
            text = str(value or "")
            return urljoin(base_url, text) if base_url else text

        # Explicit casts because `data` is untyped JSON: without them the dict
        # comprehensions below infer unknown key and value types.
        raw_accounts = cast("Mapping[str, Mapping[str, Any]]", data.get("accounts") or {})
        raw_primary = cast("Mapping[str, str]", data.get("primaryAccounts") or {})
        capabilities = cast("Mapping[str, Any]", data.get("capabilities") or {})

        accounts = {
            Id(account_id): Account.from_wire(account_id, account)
            for account_id, account in raw_accounts.items()
        }
        primary = {urn: Id(account_id) for urn, account_id in raw_primary.items()}
        return cls(
            capabilities=capabilities,
            accounts=accounts,
            primary_accounts=primary,
            username=str(data.get("username", "")),
            api_url=absolute(data.get("apiUrl")),
            download_url=absolute(data.get("downloadUrl")),
            upload_url=absolute(data.get("uploadUrl")),
            event_source_url=absolute(data.get("eventSourceUrl")),
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
