"""A C++ out-of-class member definition must hang under the class that declares it.

The parser read the owner of `void Logger::flush() {}` from the declarator text,
so a macro, a template argument list or a return type that tree-sitter folded
into the declarator became part of the owner (`APP_INLINE Sink<Mutex>`,
`std.shared_ptr<Logger> Logger`) or of the name (`level APP_NO_EXPAND`). It also
built the owner id in the current file, so a definition in `logger-inl.h` pointed
at a class that no component is, although `logger.h` declares it in the same
namespace.
"""

import tempfile
from pathlib import Path

import pytest

try:  # pragma: no cover - import guard, mirrors the other parser test modules
    import tree_sitter_cpp  # noqa: F401

    CPP_AVAILABLE = True
except ImportError:  # pragma: no cover
    CPP_AVAILABLE = False

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, ComponentType, IdScheme

pytestmark = pytest.mark.skipif(not CPP_AVAILABLE, reason="tree-sitter-cpp not installed")

LOGGER_H = """namespace app {
class Logger {
public:
    void flush();
    std::shared_ptr<Logger> clone(std::string name);
    std::vector<Sink> &sinks();
};
template <class T>
class Box {
public:
    T get() const;
    void reset();
};
template <>
class Box<int> {
public:
    Box();
    int get() const;
};
namespace sinks {
template <typename Mutex>
class Sink {
public:
    void log(int level);
};
}
}
"""

LOGGER_INL_H = """#include "logger.h"
namespace app {
APP_INLINE void Logger::flush() {
    flushed_ = true;
}
APP_INLINE std::shared_ptr<Logger> Logger::clone(std::string name) {
    return nullptr;
}
APP_INLINE std::vector<Sink> &Logger::sinks() {
    return sinks_;
}
APP_INLINE level::level_enum from_str(const std::string &name) {
    return level::info;
}
APP_INLINE level::level_enum DEFAULT_LEVEL() {
    return level::info;
}
int Logger::level APP_NO_EXPAND () const {
    return level_;
}
template <class T>
T Box<T>::get() const {
    return T();
}
Box<int>::Box() {
    value_ = 0;
}
int Box<int>::get() const {
    return value_;
}
template <>
void Box<double>::reset() {
    value_ = 0.0;
}
namespace sinks {
template <typename Mutex>
void APP_INLINE Sink<Mutex>::log(int level) {
    level_ = level;
}
}
}
"""


def short(component_id: str) -> str:
    return component_id.split("::")[-1]


def analyze(id_scheme: IdScheme = IdScheme.V2_PATH) -> list[Component]:
    directory = Path(tempfile.mkdtemp())
    (directory / "include").mkdir()
    (directory / "src").mkdir()
    (directory / "include" / "logger.h").write_text(LOGGER_H)
    (directory / "src" / "logger-inl.h").write_text(LOGGER_INL_H)
    knowledge_base = CodeAnalyzer(ComponentCatalog(None)).analyze_directory(
        directory, "space", id_scheme=id_scheme
    )
    return list(knowledge_base.components)


@pytest.fixture(scope="module")
def components() -> list[Component]:
    return analyze()


def defined(components: list[Component], name: str, owner: str = "") -> Component:
    """The component named ``name`` from the definition file, under a parent named ``owner``."""
    (component,) = [
        c
        for c in components
        if c.get_simple_name() == name
        and Path(c.file_path).name == "logger-inl.h"
        and short(c.parent_component or "").split(".")[-1] == owner
    ]
    return component


class TestTheOwnerIsReadFromTheTree:
    @pytest.mark.parametrize(
        ("name", "owner", "kind"),
        [
            ("clone", "Logger", ComponentType.METHOD),
            ("sinks", "Logger", ComponentType.METHOD),
            ("log", "Sink", ComponentType.METHOD),
            ("get", "Box", ComponentType.METHOD),
            ("get", "Box<int>", ComponentType.METHOD),
            ("Box", "Box<int>", ComponentType.CONSTRUCTOR),
            ("reset", "Box", ComponentType.METHOD),
            ("level", "Logger", ComponentType.METHOD),
        ],
        ids=[
            "macro and std return type before the owner",
            "macro and a reference return type before the owner",
            "macro and the template's own parameters",
            "member of the primary template",
            "member of a specialization",
            "constructor of a specialization",
            "specialization of a member under template <>",
            "macro after the name",
        ],
    )
    def test_macros_return_types_and_own_template_arguments_are_not_the_owner(
        self, components, name, owner, kind
    ):
        assert defined(components, name, owner).type is kind

    @pytest.mark.parametrize("name", ["from_str", "DEFAULT_LEVEL"])
    def test_a_free_function_with_a_qualified_return_type_is_a_function(self, components, name):
        assert defined(components, name, "app").type is ComponentType.FUNCTION

    def test_a_v1_space_keeps_its_ids(self):
        names = {c.get_simple_name() for c in analyze(IdScheme.V1_STEM)}

        assert "level APP_NO_EXPAND" in names


class TestTheOwnerIsTheClassInTheEnclosingNamespace:
    def test_the_definition_hangs_under_the_class_the_header_declares(self, components):
        flush = defined(components, "flush", "Logger")

        assert short(flush.parent_component) == "app.Logger"

    def test_every_method_has_a_parent_that_exists(self, components):
        ids = {short(c.id) for c in components}
        methods = [c for c in components if c.type is ComponentType.METHOD]

        assert methods
        assert all(short(method.parent_component) in ids for method in methods)
