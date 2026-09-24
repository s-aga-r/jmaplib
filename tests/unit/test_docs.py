"""The documentation must describe the library that exists.

Prose goes stale quietly. An example naming a method that was renamed, or a
keyword argument that never existed, reads exactly like one that works - and the
reader finds out at run time, which is the worst possible place. So the parts of
the docs that make checkable claims are checked here.

Three claims are worth enforcing, in rising order of what they catch:

* every Python block **parses**, so no example is malformed;
* every ``from jmap... import X`` **resolves**, so no example imports a symbol
  that is gone;
* every ``batch.<capability>.<entity>.<method>(`` **exists in the registry**,
  which is the one that catches a documented method the library cannot perform.

What this deliberately does not do is execute the examples. Most of them need a
server, and the ones that do not would need so much scaffolding that the
scaffolding would become the thing under test. The three checks above catch
staleness, which is the failure mode that actually happens.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jmap.api.namespace import attribute_name
from jmap.defaults import default_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

DOCS = Path(__file__).parents[2] / "docs"
README = Path(__file__).parents[2] / "README.md"

#: ```python fenced blocks, with their starting line so a failure can be found.
_BLOCK = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)

#: `batch.mail.email.get(` and friends.
_CALL = re.compile(r"\bbatch\.([a-z_]+)\.([a-z_]+)\.([a-z_]+)\(")

#: A method that exists but is reached through `batch.add` rather than a builder.
_ADD = re.compile(r"\bbatch\.add\(\s*[\"']([A-Za-z]+/[A-Za-z]+)[\"']")


def markdown_files() -> list[Path]:
    return [*sorted(DOCS.glob("*.md")), README]


def python_blocks(path: Path) -> Iterator[tuple[int, str]]:
    text = path.read_text()
    for match in _BLOCK.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        yield line, match.group(1)


def registry_surface() -> tuple[dict[str, dict[str, set[str]]], set[str]]:
    """``{capability_attr: {entity_attr: {method, ...}}}`` and every method name."""
    surface: dict[str, dict[str, set[str]]] = {}
    method_names: set[str] = set()
    registry = default_registry()
    for urn in registry.urns:
        for spec in registry.specs_for(urn):
            for method in spec.methods:
                method_names.add(method.name)
            if spec.attr is None:
                continue
            entities = surface.setdefault(spec.attr, {})
            for data_type in spec.data_types:
                suffixes = {
                    method.name.split("/", 1)[1]
                    for method in spec.methods
                    if method.name.split("/", 1)[0] == data_type.name
                }
                if suffixes:
                    entities[attribute_name(data_type.name)] = suffixes
    return surface, method_names


SURFACE, METHOD_NAMES = registry_surface()

#: Builders whose Python name differs from the wire suffix, or that a mixin adds
#: without the registry naming a separate method.
_ALIASES = {
    "query_changes": "queryChanges",
    # SieveScript/set drives both, through onSuccessActivateScript.
    "activate": "set",
    "deactivate": "set",
}


@pytest.mark.parametrize("path", markdown_files(), ids=lambda p: p.name)
class TestEveryDocument:
    def test_python_blocks_parse(self, path: Path) -> None:
        for line, block in python_blocks(path):
            # `...` stands in for elided code and is a valid expression, so a
            # block using it still parses.
            try:
                ast.parse(block)
            except SyntaxError as error:  # pragma: no cover - only on a bad doc
                pytest.fail(f"{path.name}:{line} does not parse: {error}")

    def test_imports_resolve(self, path: Path) -> None:
        for line, block in python_blocks(path):
            for node in ast.walk(ast.parse(block)):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                if not module.startswith("jmap"):
                    continue
                imported = __import__(module, fromlist=["_"])
                for alias in node.names:
                    if not hasattr(imported, alias.name):  # pragma: no cover
                        pytest.fail(
                            f"{path.name}:{line} imports {alias.name!r} from "
                            f"{module}, which does not export it"
                        )

    def test_documented_calls_exist(self, path: Path) -> None:
        text = path.read_text()
        for capability, entity, method in _CALL.findall(text):
            entities = SURFACE.get(capability)
            if entities is None:  # pragma: no cover - only on a bad doc
                pytest.fail(f"{path.name}: no capability exposes batch.{capability}")
            suffixes = entities.get(entity)
            if suffixes is None:  # pragma: no cover
                pytest.fail(f"{path.name}: batch.{capability} has no {entity!r}")
            wanted = _ALIASES.get(method, method)
            if wanted not in suffixes:  # pragma: no cover
                pytest.fail(
                    f"{path.name}: batch.{capability}.{entity} has no {method!r} "
                    f"(it has {sorted(suffixes)})"
                )

    def test_raw_calls_name_a_real_method(self, path: Path) -> None:
        for name in _ADD.findall(path.read_text()):
            if name not in METHOD_NAMES:  # pragma: no cover - only on a bad doc
                pytest.fail(f"{path.name}: batch.add({name!r}) names no modelled method")


class TestTheReadmeIsCurrent:
    def test_the_status_names_this_release(self):
        # It said 1.0.0 through the 1.1.0 release.
        import jmap

        assert f"**{jmap.__version__}**" in README.read_text()

    def test_it_does_not_call_shipped_features_future_work(self):
        # "Interactive OAuth acquisition ... lands after 0.1.0; for now you
        # bring a token" - long after it shipped.
        assert "for now you bring a token" not in README.read_text()


class TestTheDocumentationIsWired:
    def test_every_guide_is_linked_from_the_index(self):
        index = (DOCS / "index.md").read_text()
        for path in DOCS.glob("*.md"):
            if path.name == "index.md":
                continue
            assert path.name in index, f"{path.name} is not linked from docs/index.md"

    def test_internal_links_point_at_something(self):
        link = re.compile(r"\]\(([a-z0-9-]+\.md)(#[a-z0-9-]+)?\)")
        for path in markdown_files():
            for target, _anchor in link.findall(path.read_text()):
                assert (DOCS / target).exists(), f"{path.name} links to missing {target}"
