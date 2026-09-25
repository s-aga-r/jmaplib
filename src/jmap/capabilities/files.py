"""``urn:ietf:params:jmap:filenode`` - draft-ietf-jmap-filenode-14.

Experimental. It tracks an Internet-Draft, so it is excluded from the SemVer
promise and only resolved when the caller passes ``experimental=True``.

The capability object is unusually rich, and almost every field bounds something a
client may send. Three of them are worth reading twice:

**``maxFileNodeDepth`` is off by one.** The draft defines it as *one more than* the
maximum number of ancestors, so a value of 50 permits 49 ancestors - 50 nodes on
the root-to-leaf path. Checking ``len(ancestors) > limit`` is wrong by one, in the
permissive direction.

**``maxSizeFileNodeName`` counts UTF-8 octets**, not characters, exactly like
Sieve's script names. ``len(name)`` is wrong by up to four times on non-ASCII.

**``caseInsensitiveNames`` governs collisions only.** It does not make ``name``
filters case-insensitive, and ``nameMatch`` globs are *always* case-insensitive
regardless. Three different rules, one word.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import Field

from jmap.capabilities.spec import CapabilitySpec, DataTypeSpec, MethodKind, MethodSpec
from jmap.core.errors import CapabilityFieldError
from jmap.core.limits import LimitKey
from jmap.models.base import JMAPModel
from jmap.models.files import FileNode

FILENODE_URN: Final = "urn:ietf:params:jmap:filenode"

#: draft-14 §2.1. The floor the draft puts on ``maxSizeFileNodeName``, and the
#: safe assumption when a server omits the field.
MIN_SIZE_FILENODE_NAME: Final = 100


class FileNodeCapability(JMAPModel):
    """The per-account ``urn:ietf:params:jmap:filenode`` object (§2.1).

    The draft marks every field as MUST-contain. They are given defaults anyway,
    because a client that refuses to run against a slightly nonconformant server
    is less useful than one that assumes the most restrictive legal value.
    """

    #: One *more* than the maximum ancestor count. ``None`` means no limit.
    max_file_node_depth: int | None = None
    #: In UTF-8 octets.
    max_size_file_node_name: int = MIN_SIZE_FILENODE_NAME
    #: Each *character* of this string is forbidden in a name.
    forbidden_name_chars: str | None = None
    #: Whole names that are refused, compared case-insensitively.
    forbidden_node_names: list[str] | None = None
    #: The authoritative sort list. May carry vendor values; unknown ones are
    #: ignored rather than rejected.
    file_node_query_sort_options: list[str] = Field(default_factory=list)
    may_create_top_level_file_node: bool = False
    web_trash_url: str | None = None
    #: Governs *collision* checks only. See the module docstring.
    case_insensitive_names: bool = False
    #: RFC 6570 level 1, variable ``{id}``.
    web_url_template: str | None = None
    #: RFC 6570 level 1, variable ``{id}``. ``None`` means direct writes are
    #: unsupported and the ``Blob/upload`` + ``FileNode/set`` path is the only one.
    web_write_url_template: str | None = None

    @classmethod
    def of(cls, value: Any) -> FileNodeCapability:
        try:
            return cls.model_validate(dict(value))
        except (ValueError, TypeError):
            return cls()

    @property
    def supports_direct_write(self) -> bool:
        return bool(self.web_write_url_template)

    @property
    def max_ancestors(self) -> int | None:
        """The maximum number of *ancestors*, which is the depth limit minus one.

        Exposed separately because the off-by-one in the draft's definition is
        exactly the sort of thing that gets re-derived wrongly at each call site.
        """
        if self.max_file_node_depth is None:
            return None
        return max(0, self.max_file_node_depth - 1)


def check_node_name(name: str, capability: FileNodeCapability) -> None:
    """Reject a name the server is required to refuse (§2.1, §3.1).

    Three rules: non-empty, no forbidden characters, and within
    ``maxSizeFileNodeName`` **octets**. Each earns an ``invalidProperties``
    SetError per node otherwise, which is a slow way to learn about a slash.
    """
    if not name:
        raise CapabilityFieldError(FILENODE_URN, "name", "at least one character", "")
    forbidden = capability.forbidden_name_chars or ""
    hit = sorted({character for character in name if character in forbidden})
    if hit:
        raise CapabilityFieldError(FILENODE_URN, "forbiddenNameChars", forbidden, "".join(hit))
    octets = len(name.encode())
    if octets > capability.max_size_file_node_name:
        raise CapabilityFieldError(
            FILENODE_URN, "maxSizeFileNodeName", capability.max_size_file_node_name, octets
        )
    reserved = {entry.casefold() for entry in capability.forbidden_node_names or ()}
    if name.casefold() in reserved:
        raise CapabilityFieldError(FILENODE_URN, "forbiddenNodeNames", sorted(reserved), name)


def check_depth(ancestor_count: int, capability: FileNodeCapability) -> None:
    """Reject a placement deeper than the server allows (§2.1).

    ``ancestor_count`` is how many nodes stand between this one and the root, so
    a top-level node passes zero. The comparison is against
    :attr:`FileNodeCapability.max_ancestors`, not the raw advertised value.
    """
    limit = capability.max_ancestors
    if limit is not None and ancestor_count > limit:
        raise CapabilityFieldError(
            FILENODE_URN, "maxFileNodeDepth", capability.max_file_node_depth, ancestor_count + 1
        )


def check_sort(properties: tuple[str, ...], capability: FileNodeCapability) -> None:
    """Reject a sort the server did not advertise (§2.1).

    ``fileNodeQuerySortOptions`` is authoritative; there is no fixed list to
    hardcode, and an unadvertised comparator earns ``unsupportedSort`` for the
    whole query.
    """
    unsupported = tuple(
        name for name in properties if name not in capability.file_node_query_sort_options
    )
    if unsupported:
        raise CapabilityFieldError(
            FILENODE_URN,
            "fileNodeQuerySortOptions",
            tuple(capability.file_node_query_sort_options),
            unsupported,
        )


FILENODE: Final = CapabilitySpec(
    urn=FILENODE_URN,
    attr="files",
    reference="draft-ietf-jmap-filenode-14",
    experimental=True,
    account_value=FileNodeCapability,
    data_types=(
        DataTypeSpec(
            name="FileNode",
            model=FileNode,
            shareable=True,
            sort_options_field="fileNodeQuerySortOptions",
        ),
    ),
    methods=(
        MethodSpec(
            "FileNode/get",
            MethodKind.GET,
            chunk_by=LimitKey.GET_OBJECTS,
            extra_args={
                # §3.2.1: ancestors arrive as *extra* entries in `list`, so the
                # response is longer than `ids` and nothing marks which is which.
                "fetchParents": "also return every ancestor of the requested nodes",
            },
        ),
        MethodSpec("FileNode/changes", MethodKind.CHANGES),
        MethodSpec(
            "FileNode/query",
            MethodKind.QUERY,
            # §3.2.5: a top-level argument, not a filter condition. Putting it
            # inside `filter` earns an unknown-filter-property rejection.
            extra_args={"depth": "how many levels below the filtered parent to recurse"},
        ),
        MethodSpec("FileNode/queryChanges", MethodKind.QUERY_CHANGES),
        MethodSpec(
            "FileNode/set",
            MethodKind.SET,
            mutating=True,
            chunk_by=LimitKey.SET_OBJECTS,
            extra_args={
                "onDestroyRemoveChildren": "cascade a destroy to the whole subtree",
                "onExists": "null, replace, rename or newest, when a sibling name collides",
                "compareCaseInsensitively": "treat names as case-insensitive for this request",
            },
        ),
        MethodSpec(
            "FileNode/copy",
            MethodKind.COPY,
            mutating=True,
            # §3.2.4 names only these two - `compareCaseInsensitively` is not
            # accepted here, unlike on /set.
            extra_args={
                "onDestroyRemoveChildren": "cascade a destroy to the whole subtree",
                "onExists": "null, replace, rename or newest, when a sibling name collides",
            },
        ),
    ),
)
