"""A Rust method's parent id must name a component that exists.

The parent of a method in an `impl` block is the implemented type. The parser
used to build it from the type text, so `&'a mut Value` gave the parent
`'a mut Value`, an `impl` for a slice took the trait as its type, and an `impl`
for a type defined in another file pointed at an id that no component has.
Functions in an inline `mod` hung under a module that was not a component either.
"""

import re
import tempfile
from pathlib import Path

import pytest

try:  # pragma: no cover - import guard, mirrors the other parser test modules
    import tree_sitter_rust  # noqa: F401

    RUST_AVAILABLE = True
except ImportError:  # pragma: no cover
    RUST_AVAILABLE = False

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, ComponentType, IdScheme
from src.flow.flow_analyzer import FlowAnalyzer

pytestmark = pytest.mark.skipif(not RUST_AVAILABLE, reason="tree-sitter-rust not installed")

VALUE_RS = """pub struct Value;
pub fn defaults() -> i32 {
    2
}
"""

PARSE_RS = """use crate::value::{defaults, Value};
impl Value {
    pub fn parse(&self) -> &str {
        self.check()
    }
    pub fn check(&self) -> &str {
        "ok"
    }
}
impl<'a> std::fmt::Display for &'a mut Value {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.check())
    }
}
impl<'a, T: Clone> Index for &'a T {
    fn index(&self) {}
}
impl<'a> Index for &'a (dyn Index + 'a) {
    fn index(&self) {}
}
impl Summary for [u8] {
    fn size(&self) -> usize {
        0
    }
}
impl Summary for tagged!() {
    fn tag(&self) {}
}
pub struct Thing;
mod inner {
    pub struct Thing;
    impl super::Thing {
        pub fn outer(&self) {}
    }
    impl Thing {
        pub fn own(&self) {}
    }
}
impl inner::Thing {
    pub fn scoped(&self) {}
}
mod tests {
    pub fn helper() -> i32 {
        1
    }
}
mod defaults {
    pub const SIZE: usize = 4;
}
pub fn run() -> i32 {
    tests::helper() + defaults()
}
"""

SAME_NAME_RS = """pub fn unbounded() -> i32 {
    unbounded::inner()
}
pub mod unbounded {
    pub fn inner() -> i32 {
        1
    }
}
"""


def short(component_id: str) -> str:
    """Drop the space and the V2 path hash: `space::parse_1a2b3c4d5e6f.Value` -> `parse.Value`."""
    return re.sub(r"_[0-9a-f]{12}(?=\.|$)", "", component_id.split("::")[-1])


def analyze(files: dict[str, str], id_scheme: IdScheme = IdScheme.V2_PATH):
    directory = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (directory / name).write_text(text)
    catalog = ComponentCatalog(None)
    analyzer = CodeAnalyzer(catalog)
    knowledge_base = analyzer.analyze_directory(
        directory, "space", id_scheme=id_scheme, files=sorted(directory.iterdir())
    )
    call_graph = FlowAnalyzer(catalog).build_call_graph(
        knowledge_base.components, tree_cache=analyzer.take_parsed_trees()
    )
    components = {short(component.id): component for component in knowledge_base.components}
    return components, {short(k): [short(t) for t in v] for k, v in call_graph.items()}


@pytest.fixture(scope="module")
def analysis() -> tuple[dict[str, Component], dict[str, list[str]]]:
    return analyze({"value.rs": VALUE_RS, "parse.rs": PARSE_RS})


@pytest.fixture(scope="module")
def components(analysis) -> dict[str, Component]:
    return analysis[0]


@pytest.fixture(scope="module")
def call_graph(analysis) -> dict[str, list[str]]:
    return analysis[1]


class TestAFunctionIsAMethodOnlyInATypeBody:
    def test_a_function_in_a_module_is_a_function_that_its_path_reaches(
        self, components, call_graph
    ):
        assert components["parse.tests.helper"].type is ComponentType.FUNCTION
        assert "parse.tests.helper" in call_graph["parse.run"]


class TestTheImplTargetIsTheTypeName:
    @pytest.mark.parametrize(
        ("method", "parent"),
        [
            ("parse.Value.fmt", "parse.Value"),
            ("parse.T.index", "parse.T"),
            ("parse.Index.index", "parse.Index"),
            ("parse.slice.size", "parse.slice"),
            ("parse.tagged.tag", "parse.tagged"),
            ("parse.Thing.outer", "parse.Thing"),
            ("parse.inner.Thing.own", "parse.inner.Thing"),
            ("parse.inner.Thing.scoped", "parse.inner.Thing"),
        ],
        ids=[
            "reference with lifetime and mut",
            "type parameter",
            "a parenthesized trait object is not a tuple",
            "slice",
            "a macro in type position gives its name",
            "super:: names the type one scope out",
            "a type in the same module comes first",
            "a path names the type in that module",
        ],
    )
    def test_the_parent_is_the_name_of_the_implemented_type(self, components, method, parent):
        assert components[method].type is ComponentType.METHOD
        assert short(components[method].parent_component) == parent

    def test_a_v1_space_keeps_its_ids(self):
        components, _ = analyze({"value.rs": VALUE_RS, "parse.rs": PARSE_RS}, IdScheme.V1_STEM)

        assert "parse.'a mut Value.fmt" in components
        assert not [c for c in components.values() if c.type is ComponentType.IMPLEMENTATION]


class TestAnInlineModuleIsAComponent:
    def test_the_module_is_a_component(self, components):
        assert components["parse.tests"].type is ComponentType.MODULE

    def test_a_call_reaches_the_function_and_not_the_module_with_its_name(
        self, components, call_graph
    ):
        assert components["parse.defaults"].type is ComponentType.MODULE
        assert "value.defaults" in call_graph["parse.run"]

    def test_a_function_keeps_its_id_from_a_module_with_its_name(self):
        components, call_graph = analyze({"same.rs": SAME_NAME_RS})

        assert components["same.unbounded"].type is ComponentType.FUNCTION
        assert "same.unbounded.inner" in call_graph["same.unbounded"]


class TestAnImplForATypeDefinedElsewhereIsAComponent:
    def test_the_impl_block_is_the_component_the_methods_hang_under(self, components):
        impl_block = components["parse.Value"]

        assert impl_block.type is ComponentType.IMPLEMENTATION
        assert impl_block.attributes.signature.startswith("impl Value")
        assert short(components["parse.Value.parse"].parent_component) == "parse.Value"

    def test_every_method_has_a_parent_that_exists(self, components):
        methods = [c for c in components.values() if c.type is ComponentType.METHOD]

        assert methods
        assert all(short(method.parent_component) in components for method in methods)
