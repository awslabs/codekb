"""Rust functions in `impl` blocks and in inline modules.

The parser typed every function with a parent as a method, so a function in an
inline `mod` was a method.
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
impl Summary for [u8] {
    fn size(&self) -> usize {
        0
    }
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
