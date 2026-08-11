"""draft-ietf-jmap-filenode-14 models and the filenode capability's local gates.

Both halves are built around a rule that reads backwards. ``nodeType`` is
*inferred* when omitted, so an under-specified create silently produces a
directory rather than the file that was meant; and ``maxFileNodeDepth`` is one
more than the ancestor count it bounds, so the obvious comparison is wrong in
the permissive direction. Everything here exists to pin those down.
"""

from __future__ import annotations

import pytest

from jmap.capabilities.files import (
    FILENODE,
    FILENODE_URN,
    MIN_SIZE_FILENODE_NAME,
    FileNodeCapability,
    check_depth,
    check_node_name,
    check_sort,
)
from jmap.core.errors import CapabilityFieldError
from jmap.models.files import (
    FILE_ROLES,
    NODE_DIRECTORY,
    NODE_FILE,
    NODE_HAS_CHILDREN,
    NODE_SYMLINK,
    NODE_TYPES,
    ON_EXISTS_FAIL,
    ON_EXISTS_NEWEST,
    ON_EXISTS_RENAME,
    ON_EXISTS_REPLACE,
    FileNode,
    FilesRights,
    WriteResult,
    infer_node_type,
)


class TestInferNodeType:
    def test_a_blob_id_makes_it_a_file(self):
        assert infer_node_type({"name": "notes.txt", "blobId": "G1"}) == NODE_FILE

    def test_a_target_makes_it_a_symlink(self):
        assert infer_node_type({"name": "shortcut", "target": ["docs", "a.txt"]}) == NODE_SYMLINK

    # The trap the module exists for: no nodeType, no blobId and no target is a
    # complete, legal create object, and it makes a folder.
    def test_an_omitted_node_type_becomes_a_directory(self):
        assert infer_node_type({"name": "photos"}) == NODE_DIRECTORY

    # There is no file node with a null blobId, not even a zero-byte one, so
    # "create it empty and fill it later" produces a directory named notes.txt.
    def test_creating_an_empty_file_quietly_produces_a_folder(self):
        assert infer_node_type({"name": "notes.txt", "blobId": None}) == NODE_DIRECTORY

    def test_an_explicit_node_type_wins_over_the_inference(self):
        assert infer_node_type({"nodeType": NODE_DIRECTORY, "blobId": "G1"}) == NODE_DIRECTORY

    # A null nodeType is an absence, not a declaration, so inference still runs.
    def test_a_null_node_type_falls_through_to_the_inference(self):
        assert infer_node_type({"nodeType": None, "blobId": "G1"}) == NODE_FILE

    # §3.1's registry is extensible, so an unrecognised value is passed through
    # rather than coerced into one of the three we know.
    def test_an_unrecognised_declared_type_is_kept(self):
        assert infer_node_type({"nodeType": "socket"}) == "socket"

    # An empty target array is a value, not an absence: a truthiness test here
    # would turn a malformed symlink into a directory.
    def test_an_empty_target_array_is_still_a_symlink(self):
        assert infer_node_type({"target": []}) == NODE_SYMLINK

    def test_a_blob_id_outranks_a_target(self):
        assert infer_node_type({"blobId": "G1", "target": ["a"]}) == NODE_FILE


class TestConstants:
    def test_the_three_node_types_are_the_registered_set(self):
        assert set(NODE_TYPES) == {NODE_FILE, NODE_DIRECTORY, NODE_SYMLINK}

    def test_the_on_exists_values_are_named(self):
        # Null is the default and means "fail", so it is spelled out rather than
        # left as a bare None at call sites.
        assert ON_EXISTS_FAIL is None
        assert (ON_EXISTS_REPLACE, ON_EXISTS_RENAME, ON_EXISTS_NEWEST) == (
            "replace",
            "rename",
            "newest",
        )

    def test_the_destroy_error_type_is_named(self):
        assert NODE_HAS_CHILDREN == "nodeHasChildren"

    def test_the_registered_roles_cover_the_special_folders(self):
        assert {"root", "home", "trash", "temp"} <= FILE_ROLES


class TestFileNodeModel:
    def test_a_wire_object_parses_into_typed_rights(self):
        node = FileNode.from_wire(
            {
                "id": "f1",
                "parentId": "d1",
                "nodeType": "file",
                "blobId": "G1",
                "size": 43,
                "name": "notes.txt",
                "type": "text/plain",
                "myRights": {"mayRead": True, "mayModifyContent": True},
            }
        )
        assert node.blob_id == "G1"
        assert node.my_rights == FilesRights(may_read=True, may_modify_content=True)

    def test_the_predicates_follow_the_node_type(self):
        file_node = FileNode(node_type=NODE_FILE)
        assert (file_node.is_file, file_node.is_directory, file_node.is_symlink) == (
            True,
            False,
            False,
        )
        directory = FileNode(node_type=NODE_DIRECTORY)
        assert (directory.is_file, directory.is_directory, directory.is_symlink) == (
            False,
            True,
            False,
        )
        symlink = FileNode(node_type=NODE_SYMLINK)
        assert (symlink.is_file, symlink.is_directory, symlink.is_symlink) == (False, False, True)

    def test_an_unfetched_node_type_is_none_of_the_three(self):
        # nodeType is not in the default property set of every server response,
        # and absent must not read as "directory" the way inference does.
        node = FileNode(id="f1")
        assert node.is_file is False
        assert node.is_directory is False
        assert node.is_symlink is False

    def test_a_null_parent_means_top_level_and_is_sent(self):
        # Distinct from omitting it, which in an update says "leave it alone".
        assert FileNode(parent_id=None).to_wire() == {"parentId": None}

    def test_an_untouched_field_stays_off_the_wire(self):
        assert FileNode(name="notes.txt").to_wire() == {"name": "notes.txt"}

    # Absent means "leave it alone", explicit null means "set it to now"; a
    # client that maps unset onto None stamps the clock over every mtime it
    # touches.
    def test_a_null_modified_is_distinguishable_from_an_absent_one(self):
        assert FileNode(modified=None).to_wire() == {"modified": None}
        assert "modified" not in FileNode(name="notes.txt").to_wire()

    def test_rights_default_to_denying_everything(self):
        rights = FilesRights()
        assert rights.may_read is False
        assert rights.may_add_children is False
        assert rights.may_rename is False
        assert rights.may_delete is False
        assert rights.may_modify_content is False
        assert rights.may_share is False


class TestAbsoluteSymlink:
    def test_a_node_with_no_target_is_not_absolute(self):
        assert FileNode(node_type=NODE_SYMLINK, target=None).is_absolute_symlink is False

    def test_an_empty_target_is_not_absolute(self):
        assert FileNode(node_type=NODE_SYMLINK, target=[]).is_absolute_symlink is False

    # target is an array of path *elements*: an empty first element is what
    # "absolute from the tree root" looks like, and it is exactly what a naive
    # "/".join round trip destroys.
    def test_an_empty_first_element_means_the_tree_root(self):
        assert FileNode(target=[""]).is_absolute_symlink is True
        assert FileNode(target=["", "docs", "a.txt"]).is_absolute_symlink is True

    def test_a_named_first_element_is_relative(self):
        assert FileNode(target=["a"]).is_absolute_symlink is False
        assert FileNode(target=["..", "sibling"]).is_absolute_symlink is False


class TestWriteResult:
    def test_a_direct_write_answers_with_the_stored_blob(self):
        # Not a method response: no sessionState and no newState, so the node's
        # `changed` has moved and any cached copy has to be re-fetched.
        result = WriteResult.from_wire({"blobId": "G2", "size": 12, "type": "text/plain"})
        assert result.blob_id == "G2"
        assert result.size == 12
        assert result.type == "text/plain"


class TestCapabilityObject:
    def test_the_advertised_fields_are_read(self):
        capability = FileNodeCapability.of(
            {
                "maxFileNodeDepth": 50,
                "maxSizeFileNodeName": 255,
                "forbiddenNameChars": "/\\",
                "forbiddenNodeNames": ["CON"],
                "fileNodeQuerySortOptions": ["name", "size"],
                "mayCreateTopLevelFileNode": True,
                "webTrashUrl": "https://example.com/trash",
                "caseInsensitiveNames": True,
                "webUrlTemplate": "https://example.com/file/{id}",
                "webWriteUrlTemplate": "https://example.com/write/{id}",
            }
        )
        assert capability.max_size_file_node_name == 255
        assert capability.forbidden_name_chars == "/\\"
        assert capability.file_node_query_sort_options == ["name", "size"]
        assert capability.may_create_top_level_file_node is True
        assert capability.case_insensitive_names is True

    def test_an_absent_object_falls_back_to_the_draft_floor(self):
        # §2.1 puts a floor of 100 octets on the name limit, so that is what
        # silence means rather than "unknown".
        capability = FileNodeCapability.of({})
        assert capability.max_size_file_node_name == MIN_SIZE_FILENODE_NAME
        assert MIN_SIZE_FILENODE_NAME == 100

    def test_the_defaults_are_the_most_restrictive_legal_values(self):
        capability = FileNodeCapability.of({})
        assert capability.may_create_top_level_file_node is False
        assert capability.case_insensitive_names is False
        assert capability.file_node_query_sort_options == []
        assert capability.forbidden_name_chars is None
        assert capability.forbidden_node_names is None

    def test_a_malformed_object_degrades_rather_than_failing_the_session(self):
        # One nonconformant capability object should not make an otherwise
        # working server unusable.
        assert FileNodeCapability.of({"maxSizeFileNodeName": "lots"}).max_size_file_node_name == (
            MIN_SIZE_FILENODE_NAME
        )
        assert FileNodeCapability.of(7).max_size_file_node_name == MIN_SIZE_FILENODE_NAME

    def test_direct_write_support_follows_the_write_template(self):
        # No template means Blob/upload plus FileNode/set is the only path, not
        # that writes are unsupported.
        assert FileNodeCapability.of({}).supports_direct_write is False
        assert FileNodeCapability.of({"webWriteUrlTemplate": None}).supports_direct_write is False
        assert (
            FileNodeCapability.of(
                {"webWriteUrlTemplate": "https://example.com/write/{id}"}
            ).supports_direct_write
            is True
        )


class TestMaxAncestors:
    # The draft defines maxFileNodeDepth as one *more* than the maximum ancestor
    # count, so 50 permits 49 ancestors - 50 nodes on the root-to-leaf path.
    def test_the_depth_limit_is_one_more_than_the_ancestor_limit(self):
        assert FileNodeCapability.of({"maxFileNodeDepth": 50}).max_ancestors == 49

    def test_no_advertised_depth_means_no_ancestor_limit(self):
        assert FileNodeCapability.of({}).max_ancestors is None
        assert FileNodeCapability.of({"maxFileNodeDepth": None}).max_ancestors is None

    def test_it_floors_at_zero(self):
        # A server advertising 0 or 1 permits top-level nodes only; -1 ancestors
        # would make check_depth reject even those.
        assert FileNodeCapability.of({"maxFileNodeDepth": 1}).max_ancestors == 0
        assert FileNodeCapability.of({"maxFileNodeDepth": 0}).max_ancestors == 0


class TestDepthGate:
    def test_a_top_level_node_has_no_ancestors_and_passes(self):
        check_depth(0, FileNodeCapability.of({"maxFileNodeDepth": 1}))

    def test_the_last_permitted_ancestor_count_passes(self):
        check_depth(49, FileNodeCapability.of({"maxFileNodeDepth": 50}))

    def test_one_ancestor_further_is_refused(self):
        with pytest.raises(CapabilityFieldError, match="maxFileNodeDepth") as excinfo:
            check_depth(50, FileNodeCapability.of({"maxFileNodeDepth": 50}))
        assert excinfo.value.urn == FILENODE_URN
        # Reported as a depth on both sides, matching the field it names, rather
        # than mixing an advertised depth with a requested ancestor count.
        assert excinfo.value.advertised == 50
        assert excinfo.value.requested == 51

    def test_no_advertised_depth_means_no_check(self):
        check_depth(10**6, FileNodeCapability.of({}))


class TestNodeNameGate:
    def test_a_legal_name_passes(self):
        check_node_name("notes.txt", FileNodeCapability.of({}))

    def test_an_empty_name_is_refused(self):
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_node_name("", FileNodeCapability.of({}))
        assert excinfo.value.urn == FILENODE_URN
        assert excinfo.value.field == "name"

    def test_a_forbidden_character_is_refused(self):
        capability = FileNodeCapability.of({"forbiddenNameChars": "/\\"})
        with pytest.raises(CapabilityFieldError, match="forbiddenNameChars"):
            check_node_name("a/b", capability)

    def test_every_offending_character_is_reported_once(self):
        # A repeated separator should not produce a repeated complaint, and the
        # order has to be stable for the message to be comparable.
        capability = FileNodeCapability.of({"forbiddenNameChars": "/\\"})
        with pytest.raises(CapabilityFieldError) as excinfo:
            check_node_name("a/b\\c/d", capability)
        assert excinfo.value.requested == "/\\"

    def test_a_server_advertising_no_forbidden_characters_permits_them_all(self):
        check_node_name("a/b", FileNodeCapability.of({}))

    def test_the_length_limit_counts_octets(self):
        # §2.1 gives it in octets, so len() is wrong by up to a factor of four:
        # four CJK characters are twelve octets and blow an eight-octet limit.
        capability = FileNodeCapability.of({"maxSizeFileNodeName": 8})
        check_node_name("abcdefgh", capability)  # 8 characters, 8 octets
        with pytest.raises(CapabilityFieldError, match="maxSizeFileNodeName") as excinfo:
            check_node_name("日本語訳", capability)  # 4 characters, 12 octets
        assert excinfo.value.requested == 12

    def test_a_reserved_name_is_matched_case_insensitively(self):
        capability = FileNodeCapability.of({"forbiddenNodeNames": ["CON"]})
        for name in ("con", "CON", "Con"):
            with pytest.raises(CapabilityFieldError, match="forbiddenNodeNames"):
                check_node_name(name, capability)

    def test_a_name_merely_containing_a_reserved_word_passes(self):
        # Whole names are reserved, not substrings.
        check_node_name("console.log", FileNodeCapability.of({"forbiddenNodeNames": ["CON"]}))

    def test_a_server_advertising_no_reserved_names_permits_them_all(self):
        check_node_name("CON", FileNodeCapability.of({}))


class TestSortGate:
    def test_an_advertised_comparator_passes(self):
        check_sort(("name",), FileNodeCapability.of({"fileNodeQuerySortOptions": ["name", "size"]}))

    def test_sorting_by_nothing_passes(self):
        check_sort((), FileNodeCapability.of({}))

    def test_an_unadvertised_comparator_is_refused(self):
        capability = FileNodeCapability.of({"fileNodeQuerySortOptions": ["name"]})
        with pytest.raises(CapabilityFieldError, match="fileNodeQuerySortOptions") as excinfo:
            check_sort(("name", "size"), capability)
        assert excinfo.value.urn == FILENODE_URN
        # Only the offending comparators are named; the caller does not have to
        # diff the two lists to find out which one to drop.
        assert excinfo.value.requested == ("size",)

    def test_the_advertised_list_is_authoritative(self):
        # Even the obvious comparators are refused by a server that advertises
        # none: there is no fixed set to fall back on.
        with pytest.raises(CapabilityFieldError):
            check_sort(("name",), FileNodeCapability.of({}))


class TestSpec:
    def test_the_capability_is_experimental(self):
        # It tracks an Internet-Draft, so it is outside the SemVer promise and
        # only resolved when the caller opts in.
        assert FILENODE.experimental is True
        assert FILENODE.urn == FILENODE_URN
        assert FILENODE.attr == "files"
        assert FILENODE.account_value is FileNodeCapability

    def test_the_file_node_type_is_shareable(self):
        data_type = FILENODE.data_type("FileNode")
        assert data_type is not None
        assert data_type.model is FileNode
        assert data_type.shareable is True

    def test_copy_does_not_accept_case_insensitive_comparison(self):
        # §3.2.4 names only two arguments for /copy; sending the third earns an
        # unknown-argument rejection even though /set takes it.
        set_method = FILENODE.method("FileNode/set")
        copy_method = FILENODE.method("FileNode/copy")
        assert set_method is not None
        assert copy_method is not None
        assert set(set_method.extra_args) == {
            "onDestroyRemoveChildren",
            "onExists",
            "compareCaseInsensitively",
        }
        assert set(copy_method.extra_args) == {"onDestroyRemoveChildren", "onExists"}

    def test_the_writing_methods_are_marked_mutating(self):
        for name in ("FileNode/set", "FileNode/copy"):
            method = FILENODE.method(name)
            assert method is not None
            assert method.mutating is True

    def test_get_can_ask_for_ancestors(self):
        method = FILENODE.method("FileNode/get")
        assert method is not None
        assert set(method.extra_args) == {"fetchParents"}

    def test_query_takes_depth_as_a_top_level_argument(self):
        # §3.2.5: inside `filter` it is an unknown filter property, not a depth.
        method = FILENODE.method("FileNode/query")
        assert method is not None
        assert set(method.extra_args) == {"depth"}

    def test_the_full_method_set_is_declared(self):
        assert {method.name for method in FILENODE.methods} == {
            "FileNode/get",
            "FileNode/changes",
            "FileNode/query",
            "FileNode/queryChanges",
            "FileNode/set",
            "FileNode/copy",
        }
