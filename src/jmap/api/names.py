"""``/set`` builders that check the names in them first.

A mailbox, a Sieve script and a file node each have a name the server must
refuse for reasons the account advertises - too many octets, a forbidden
character, a reserved name - and each refusal costs that object a SetError that
says little more than ``invalidProperties``. These are the standard ``/set``,
checking every literal name in ``create`` and ``update`` first. A name given as
a back-reference, or a call queued with ``batch.add``, is the server's to judge.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeAlias

from jmap.api.entity import Creation, Settable, builder
from jmap.capabilities.files import FILENODE_URN, FileNodeCapability, check_node_name
from jmap.capabilities.mail import MAIL_URN, MailCapability, check_mailbox_name
from jmap.capabilities.sieve import SIEVE_URN, SieveAccountCapability, check_script_name
from jmap.core.errors import CapabilityFieldError
from jmap.core.invocation import Handle, ResultRef
from jmap.models.base import UNSET, JMAPModel, Unset
from jmap.models.responses import SetResponse


class _NameChecked(Settable[Any]):
    """The standard ``/set``, checking each name before the call is queued."""

    __slots__ = ()

    @builder
    def set(
        self,
        *,
        create: Mapping[str, Creation] | ResultRef[Any] | Unset | None = UNSET,
        update: Mapping[str, Mapping[str, Any]] | ResultRef[Any] | Unset | None = UNSET,
        **extra: Any,
    ) -> Handle[SetResponse[Any]]:
        """Create, update and destroy in one atomic call, names checked first.

        See :meth:`jmap.api.entity.Settable.set` for the rest.
        """
        created = _objects(create)
        for changed in (*created, *_objects(update)):
            name = changed.get("name")
            if isinstance(name, str):
                self._check_name(name)
        self._check_created(created)
        return super().set(create=create, update=update, **extra)

    def _check_name(self, name: str) -> None:
        raise NotImplementedError

    def _check_created(self, created: list[Mapping[str, Any]]) -> None:
        """Anything else a new object must satisfy; nothing by default."""


class MailboxSettable(_NameChecked):
    """``Mailbox/set`` (RFC 8621 §2.5)."""

    __slots__ = ()

    def _check_name(self, name: str) -> None:
        check_mailbox_name(name, self._capability())

    def _check_created(self, created: list[Mapping[str, Any]]) -> None:
        """A mailbox with no parent needs ``mayCreateTopLevelMailbox`` (§1.3.1)."""
        if self._capability().may_create_top_level_mailbox is not False:
            return
        if any(mailbox.get("parentId") is None for mailbox in created):
            raise CapabilityFieldError(
                MAIL_URN, "mayCreateTopLevelMailbox", False, "a mailbox with no parentId"
            )

    def _capability(self) -> MailCapability:
        return MailCapability.of(self._batch.capability_value(MAIL_URN))


class SieveScriptSettable(_NameChecked):
    """``SieveScript/set`` (RFC 9661 §2.4)."""

    __slots__ = ()

    def _check_name(self, name: str) -> None:
        capability = SieveAccountCapability.of(self._batch.capability_value(SIEVE_URN))
        check_script_name(name, capability)


class FileNodeSettable(_NameChecked):
    """``FileNode/set`` (draft-ietf-jmap-filenode)."""

    __slots__ = ()

    def _check_name(self, name: str) -> None:
        check_node_name(name, FileNodeCapability.of(self._batch.capability_value(FILENODE_URN)))


#: A ``create`` or an ``update`` argument, as a builder takes it.
_Changes: TypeAlias = Mapping[str, Creation] | Mapping[str, Mapping[str, Any]]


def _objects(changes: _Changes | ResultRef[Any] | Unset | None) -> list[Mapping[str, Any]]:
    """The objects to create, or the patches to apply, as wire mappings."""
    if not isinstance(changes, Mapping):
        return []
    return [
        change.to_wire() if isinstance(change, JMAPModel) else change for change in changes.values()
    ]
