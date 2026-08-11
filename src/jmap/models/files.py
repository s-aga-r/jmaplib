"""File storage (draft-ietf-jmap-filenode-14).

Experimental: this tracks an Internet-Draft, so it is excluded from the SemVer
promise and only resolved when the caller opts in.

A FileNode is a discriminated union on ``nodeType``, and the discriminant is
*inferred* when omitted - which is the first trap. Omit ``nodeType``, omit
``blobId``, omit ``target``, and the server creates a **directory**. There is no
such thing as a file node with a null ``blobId``: even a zero-byte file needs a
real blob, so "create an empty file and fill it later" silently produces a folder.

Two more that cost data rather than confusion:

**``modified`` and ``accessed`` distinguish absent from null.** Absent in an update
means leave it alone; an explicit ``null`` means set it to now. A client that maps
"unset" onto ``None`` stamps the server clock over every preserved mtime it
touches. They are also *client*-managed - the server never bumps them - while
``changed`` is the server's own and is read-only.

**``target`` is an array of path elements, not a path string.** An empty first
element means "absolute from the tree root"; ``".."`` means parent. Joining with
``/`` and splitting back loses that distinction the moment a name legitimately
contains a separator.
"""

from __future__ import annotations

from typing import Any

from jmap.models.base import JMAPModel

#: draft-14 §3.1. The legal ``nodeType`` values; the registry is extensible, so an
#: unrecognised one is kept rather than rejected.
NODE_FILE = "file"
NODE_DIRECTORY = "directory"
NODE_SYMLINK = "symlink"

NODE_TYPES = frozenset({NODE_FILE, NODE_DIRECTORY, NODE_SYMLINK})

#: §3.2.3. What to do when a sibling of the same name already exists.
ON_EXISTS_FAIL = None
ON_EXISTS_REPLACE = "replace"
ON_EXISTS_RENAME = "rename"
#: Added in draft-14: replace only if the incoming ``modified`` is strictly later.
ON_EXISTS_NEWEST = "newest"

#: §3.2.3 / §10.2. Destroying a node that still has children, with
#: ``onDestroyRemoveChildren`` false.
NODE_HAS_CHILDREN = "nodeHasChildren"

#: §10.5. Registered roles. Clients MUST ignore ones they do not recognise.
FILE_ROLES = frozenset(
    {
        "root",
        "home",
        "temp",
        "trash",
        "documents",
        "downloads",
        "music",
        "pictures",
        "videos",
    }
)


class FilesRights(JMAPModel):
    """What the requesting user may do with a node (draft-14 §3.1).

    Derived and inherited: a ``shareWith`` change on an ancestor changes these on
    every descendant, and the server reports those descendants as changed even
    though nothing about them otherwise moved.
    """

    may_read: bool = False
    may_add_children: bool = False
    may_rename: bool = False
    may_delete: bool = False
    #: Covers ``blobId``, ``type``, ``target``, ``modified``, ``accessed`` and
    #: ``executable`` - the content and its metadata, as one permission.
    may_modify_content: bool = False
    may_share: bool = False


class FileNode(JMAPModel):
    """A file, directory or symlink (draft-ietf-jmap-filenode-14 §3.1)."""

    id: str | None = None
    #: ``None`` means top level, which is gated by ``mayCreateTopLevelFileNode``
    #: rather than by any node's rights.
    parent_id: str | None = None
    #: Immutable. Inferred on create when omitted - see the module docstring.
    node_type: str | None = None
    #: Non-null exactly when this is a file, including a zero-byte one.
    blob_id: str | None = None
    #: Path *elements* for a symlink, not a path string.
    target: list[str] | None = None
    #: Server-set, though a client may supply a value that matches the blob.
    size: int | None = None
    name: str | None = None
    #: The media type. Client-asserted, and §9.5 says never to trust it for a
    #: security decision.
    type: str | None = None
    created: str | None = None
    #: Client-managed. See the module docstring on absent vs null.
    modified: str | None = None
    #: Client-managed.
    accessed: str | None = None
    #: Server-set, and the only one of the four timestamps the server maintains.
    changed: str | None = None
    executable: bool | None = None
    #: Per-user, and defaults to true.
    is_subscribed: bool | None = None
    #: Server-set.
    my_rights: FilesRights | None = None
    #: ``None`` is ambiguous: either nobody has access *or* you lack ``mayShare``
    #: and cannot see who does. Never render it as "nobody".
    share_with: dict[str, FilesRights] | None = None
    role: str | None = None

    @property
    def is_file(self) -> bool:
        return self.node_type == NODE_FILE

    @property
    def is_directory(self) -> bool:
        return self.node_type == NODE_DIRECTORY

    @property
    def is_symlink(self) -> bool:
        return self.node_type == NODE_SYMLINK

    @property
    def is_absolute_symlink(self) -> bool:
        """Whether a symlink's target starts at the tree root.

        Signalled by an empty first path element, which is exactly the thing a
        naive ``"/".join`` round trip destroys.
        """
        return self.target is not None and len(self.target) > 0 and self.target[0] == ""


class WriteResult(JMAPModel):
    """The answer to a direct ``PUT``/``PATCH`` (draft-14 §4.1, §4.2).

    Deliberately *not* a JMAP method response: no ``sessionState``, no
    ``newState``. After one of these the node's ``changed`` has moved and any
    cached copy is stale, so a client that needs the rest of the object must
    re-fetch it.
    """

    blob_id: str | None = None
    size: int | None = None
    type: str | None = None


def infer_node_type(creation: dict[str, Any]) -> str:
    """What the server will decide ``nodeType`` is, given a create object (§3.1).

    Reproduced here so a client can see the answer before sending. The order
    matters and the fallback is ``directory``, which is how "create an empty file"
    quietly becomes a folder.
    """
    declared = creation.get("nodeType")
    if isinstance(declared, str):
        return declared
    if creation.get("blobId") is not None:
        return NODE_FILE
    if creation.get("target") is not None:
        return NODE_SYMLINK
    return NODE_DIRECTORY
