"""
Integrated parser and call-graph regression tests.

Covers the complete parsing pipeline: component extraction + call graph
building + knowledge-base round-trip through S3. Intended to catch
regressions in:
  - Refactoring
  - Bug fixes
  - New language support or parser features
  - Flow analyzer changes

Note on "snapshots": the regression tests here store their baseline in mocked
S3 (via moto + our S3 Vectors mock in conftest.py) rather than in on-disk
JSON files. The `test_save_*_snapshot` tests write the current output, and
the `test_*_regression` tests verify the round-trip is stable within the
same test session. For persistent golden files, see tests/snapshots/ (not
used today).
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest
import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
import tree_sitter_typescript as tstype

try:
    import tree_sitter_c_sharp as tscsharp

    CSHARP_AVAILABLE = True
except ImportError:
    tscsharp = None
    CSHARP_AVAILABLE = False

try:
    import tree_sitter_rust as tsrust

    RUST_AVAILABLE = True
except ImportError:
    tsrust = None
    RUST_AVAILABLE = False

try:
    import tree_sitter_cpp as tscpp

    CPP_AVAILABLE = True
except ImportError:
    tscpp = None
    CPP_AVAILABLE = False

try:
    import tree_sitter_c as tsc

    C_AVAILABLE = True
except ImportError:
    tsc = None
    C_AVAILABLE = False

try:
    import tree_sitter_php as tsphp

    PHP_AVAILABLE = True
except ImportError:
    tsphp = None
    PHP_AVAILABLE = False

try:
    import tree_sitter_ruby as tsruby

    RUBY_AVAILABLE = True
except ImportError:
    tsruby = None
    RUBY_AVAILABLE = False

try:
    import tree_sitter_hcl as tshcl

    HCL_AVAILABLE = True
except ImportError:
    tshcl = None
    HCL_AVAILABLE = False

try:
    import tree_sitter_dart as tsdart

    DART_AVAILABLE = True
except ImportError:
    tsdart = None
    DART_AVAILABLE = False

from tree_sitter import Language, Parser

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, ComponentType, KnowledgeBase, Visibility, IdScheme
from src.flow.flow_analyzer import FlowAnalyzer
from src.parsers import (
    CSharpParser,
    DartParser,
    CppParser,
    JavaParser,
    PhpParser,
    PythonParser,
    RubyParser,
    RustParser,
    TerraformParser,
    TypeScriptParser,
)
from src.parsers.javascript_parser import JavaScriptParser
from src.persistence.knowledge_base_manager import KnowledgeBaseManager


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"


def component_to_dict(component: Component) -> Dict[str, Any]:
    """Convert component to a comparable dict for assertions / diffs."""
    return {
        "id": component.id,
        "type": component.type.value,
        "line_number": component.line_number,
        "end_line_number": component.end_line_number,
        "language": component.language,
        "parent_component": component.parent_component,
        "attributes": {
            "visibility": component.attributes.visibility.value
            if component.attributes.visibility
            else None,
            "parameters": [
                {"name": p.name, "required": p.required} for p in component.attributes.parameters
            ]
            if component.attributes.parameters
            else [],
            "line_count": component.attributes.line_count,
        },
    }


def parse_and_analyze(
    file_path: Path, parser_class, language_obj
) -> tuple[List[Component], Dict[str, List[str]]]:
    """Parse a single file, extract components, and build its call graph."""
    parser = Parser(language_obj)
    lang_parser = parser_class()
    # Integration tests assert readable, stable V1 ids; V2 id construction is
    # covered by test/test_id_scheme.py.
    lang_parser._scheme = IdScheme.V1_STEM

    source_code = file_path.read_bytes()
    tree = parser.parse(source_code)

    components = lang_parser.extract_components(tree.root_node, source_code, file_path)

    catalog = ComponentCatalog()
    for comp in components:
        catalog.add_component(comp)

    flow_analyzer = FlowAnalyzer(catalog)
    call_graph = flow_analyzer.build_call_graph(components)

    # Sort for consistent comparison across runs.
    call_graph_sorted = {k: sorted(v) for k, v in call_graph.items()}

    return components, call_graph_sorted


class TestPythonIntegration:
    """Test Python parser + call graph integration."""

    @pytest.fixture
    def python_lang(self):
        return Language(tspython.language())

    def test_python_components(self, python_lang):
        file_path = FIXTURES_DIR / "parser_test_python.py"
        components, _ = parse_and_analyze(file_path, PythonParser, python_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        # Constants
        assert "parser_test_python.MAX_RETRIES" in ids
        assert "parser_test_python.DEFAULT_TIMEOUT" in ids

        # Lambda / function assignments
        assert "parser_test_python.validate_input" in ids

        # Functions
        assert "parser_test_python.helper_function" in ids
        assert "parser_test_python.another_helper" in ids
        assert "parser_test_python.main_function" in ids
        assert "parser_test_python._private_helper" in ids

        # Classes and methods
        assert "parser_test_python.Calculator" in ids
        assert "parser_test_python.Calculator.__init__" in ids
        assert "parser_test_python.Calculator.add" in ids
        assert "parser_test_python.Calculator.multiply" in ids
        assert "parser_test_python.Calculator._validate" in ids
        assert "parser_test_python.Calculator.calculate" in ids
        assert "parser_test_python.Calculator.process_with_nested" in ids

        # Nested function
        assert "parser_test_python.Calculator.process_with_nested.filter_valid" in ids

        # Second class
        assert "parser_test_python.DataProcessor" in ids
        assert "parser_test_python.DataProcessor.__init__" in ids
        assert "parser_test_python.DataProcessor.process" in ids

        # Parameters (default params)
        main_func = next(c for c in result if c["id"] == "parser_test_python.main_function")
        assert len(main_func["attributes"]["parameters"]) == 2
        assert main_func["attributes"]["parameters"][0]["required"] is True
        assert main_func["attributes"]["parameters"][1]["required"] is False

        # Visibility
        private_func = next(c for c in result if c["id"] == "parser_test_python._private_helper")
        assert private_func["attributes"]["visibility"] == "private"

        private_method = next(
            c for c in result if c["id"] == "parser_test_python.Calculator._validate"
        )
        assert private_method["attributes"]["visibility"] == "private"

    def test_python_call_graph(self, python_lang):
        file_path = FIXTURES_DIR / "parser_test_python.py"
        _, call_graph = parse_and_analyze(file_path, PythonParser, python_lang)

        assert "parser_test_python.main_function" in call_graph
        calls = call_graph["parser_test_python.main_function"]
        assert "parser_test_python.helper_function" in calls
        assert "parser_test_python.another_helper" in calls

        assert "parser_test_python.Calculator.calculate" in call_graph
        calc_calls = call_graph["parser_test_python.Calculator.calculate"]
        assert "parser_test_python.Calculator._validate" in calc_calls
        assert "parser_test_python.Calculator.add" in calc_calls
        assert "parser_test_python.Calculator.multiply" in calc_calls

        assert "parser_test_python.Calculator.process_with_nested" in call_graph
        nested_calls = call_graph["parser_test_python.Calculator.process_with_nested"]
        assert "parser_test_python.Calculator.process_with_nested.filter_valid" in nested_calls

        assert "parser_test_python.Calculator.process_with_nested.filter_valid" in call_graph
        filter_calls = call_graph["parser_test_python.Calculator.process_with_nested.filter_valid"]
        assert "parser_test_python.Calculator._validate" in filter_calls

        assert "parser_test_python.DataProcessor.__init__" in call_graph
        init_calls = call_graph["parser_test_python.DataProcessor.__init__"]
        assert "parser_test_python.Calculator" in init_calls

        assert "parser_test_python.DataProcessor.process" in call_graph
        processor_calls = call_graph["parser_test_python.DataProcessor.process"]
        assert "parser_test_python._private_helper" in processor_calls
        assert "parser_test_python.validate_input" in processor_calls
        assert "parser_test_python.Calculator.calculate" in processor_calls


class TestTypeScriptIntegration:
    """Test TypeScript parser + call graph integration."""

    @pytest.fixture
    def typescript_lang(self):
        return Language(tstype.language_typescript())

    def test_typescript_components(self, typescript_lang):
        file_path = FIXTURES_DIR / "parser_test_typescript.ts"
        components, _ = parse_and_analyze(file_path, TypeScriptParser, typescript_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        assert "parser_test_typescript.MAX_RETRIES" in ids
        assert "parser_test_typescript.DEFAULT_TIMEOUT" in ids

        assert "parser_test_typescript.validateInput" in ids

        assert "parser_test_typescript.helperFunction" in ids
        assert "parser_test_typescript.anotherHelper" in ids
        assert "parser_test_typescript.mainFunction" in ids
        assert "parser_test_typescript._privateHelper" in ids

        assert "parser_test_typescript.CalculatorConfig" in ids

        assert "parser_test_typescript.Calculator" in ids
        assert "parser_test_typescript.Calculator.constructor" in ids
        assert "parser_test_typescript.Calculator.add" in ids
        assert "parser_test_typescript.Calculator.multiply" in ids
        assert "parser_test_typescript.Calculator._validate" in ids
        assert "parser_test_typescript.Calculator.calculate" in ids
        assert "parser_test_typescript.Calculator.processWithNested" in ids

        assert "parser_test_typescript.Calculator.filterValid" in ids

        assert "parser_test_typescript.DataProcessor" in ids
        assert "parser_test_typescript.DataProcessor.constructor" in ids
        assert "parser_test_typescript.DataProcessor.process" in ids

        private_func = next(c for c in result if c["id"] == "parser_test_typescript._privateHelper")
        assert private_func["attributes"]["visibility"] == "private"

        private_method = next(
            c for c in result if c["id"] == "parser_test_typescript.Calculator._validate"
        )
        assert private_method["attributes"]["visibility"] == "private"

    def test_typescript_call_graph(self, typescript_lang):
        file_path = FIXTURES_DIR / "parser_test_typescript.ts"
        _, call_graph = parse_and_analyze(file_path, TypeScriptParser, typescript_lang)

        assert "parser_test_typescript.mainFunction" in call_graph
        calls = call_graph["parser_test_typescript.mainFunction"]
        assert "parser_test_typescript.helperFunction" in calls
        assert "parser_test_typescript.anotherHelper" in calls

        assert "parser_test_typescript.DataProcessor.process" in call_graph
        processor_calls = call_graph["parser_test_typescript.DataProcessor.process"]
        assert "parser_test_typescript._privateHelper" in processor_calls
        assert "parser_test_typescript.validateInput" in processor_calls


class TestIntegrationRegression:
    """Regression tests that round-trip through the knowledge base + mock S3."""

    def _analyze_and_export(self, space_id: str) -> KnowledgeBase:
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id=space_id, id_scheme=IdScheme.V1_STEM)
        manager = KnowledgeBaseManager()
        manager.export_knowledge_base(kb)
        return kb

    def test_save_python_snapshot(self):
        """Generate Python complete snapshot."""
        self._analyze_and_export("fixtures-python")

    def test_save_typescript_snapshot(self):
        """Generate TypeScript complete snapshot."""
        self._analyze_and_export("fixtures-typescript")

    def test_python_regression(self):
        """Export and re-import the Python KB, assert equality."""
        kb_current = self._analyze_and_export("fixtures-python")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-python")

        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"

    def test_typescript_regression(self):
        """Export and re-import the TypeScript KB, assert equality."""
        kb_current = self._analyze_and_export("fixtures-typescript")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-typescript")

        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"


class TestJavaIntegration:
    """Test Java parser + call graph integration."""

    @pytest.fixture
    def java_lang(self):
        return Language(tsjava.language())

    def test_java_components(self, java_lang):
        file_path = FIXTURES_DIR / "parser_test_java.java"
        components, _ = parse_and_analyze(file_path, JavaParser, java_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        assert "parser_test_java.UserService" in ids
        assert "parser_test_java.UserService.MAX_USERS" in ids
        assert "parser_test_java.UserService.addUser" in ids
        assert "parser_test_java.UserService.notifyAdmin" in ids
        assert "parser_test_java.UserRepository" in ids
        assert "parser_test_java.UserRole" in ids

    def test_java_call_graph(self, java_lang):
        file_path = FIXTURES_DIR / "parser_test_java.java"
        _, call_graph = parse_and_analyze(file_path, JavaParser, java_lang)

        assert "parser_test_java.UserService.addUser" in call_graph
        calls = call_graph["parser_test_java.UserService.addUser"]
        assert "parser_test_java.UserService.notifyAdmin" in calls

    def test_java_field_receiver_call_graph(self, java_lang):
        """A call through a class field (``userService.addUser()``) resolves to
        the field's declared type, not to whatever shares the receiver's name.

        Regression: extract_call_name previously returned the receiver
        identifier (``userService``) instead of the method, so the real callee
        was dropped and the name was mis-bound to any same-named component.
        """
        file_path = FIXTURES_DIR / "parser_test_java.java"
        _, call_graph = parse_and_analyze(file_path, JavaParser, java_lang)

        calls = call_graph["parser_test_java.UserProcessor.processUser"]
        # Field-receiver call: resolved via the `userService` field's type.
        assert "parser_test_java.UserService.addUser" in calls
        # Static call keeps the method, not just the class.
        assert "parser_test_java.StringHelper.clean" in calls
        # The receiver name must never leak in as a bogus edge.
        assert "parser_test_java.UserProcessor" not in calls


class TestJavaRegression:
    """Java regression test: round-trip through KB + mock S3."""

    def _analyze_and_export(self, space_id: str) -> KnowledgeBase:
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id=space_id, id_scheme=IdScheme.V1_STEM)
        manager = KnowledgeBaseManager()
        manager.export_knowledge_base(kb)
        return kb

    def test_save_java_snapshot(self):
        self._analyze_and_export("fixtures-java")

    def test_java_regression(self):
        kb_current = self._analyze_and_export("fixtures-java")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-java")
        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"


@pytest.mark.skipif(not CSHARP_AVAILABLE, reason="tree_sitter_c_sharp not installed")
class TestCSharpIntegration:
    """Test C# parser + call graph integration."""

    @pytest.fixture
    def csharp_lang(self):
        return Language(tscsharp.language())

    def test_csharp_components(self, csharp_lang):
        file_path = FIXTURES_DIR / "parser_test_csharp.cs"
        components, _ = parse_and_analyze(file_path, CSharpParser, csharp_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        # Namespace prefix is threaded through component IDs.
        assert "Example.Test.StringHelper" in ids
        assert "Example.Test.StringHelper.Clean" in ids

        # Class + members
        assert "Example.Test.UserService" in ids
        assert "Example.Test.UserService.MAX_USERS" in ids
        assert "Example.Test.UserService.DEFAULT_ROLE" in ids
        assert "Example.Test.UserService.UserService" in ids  # constructor
        assert "Example.Test.UserService.AddUser" in ids
        assert "Example.Test.UserService.NotifyAdmin" in ids
        assert "Example.Test.UserService.GetVersion" in ids
        assert "Example.Test.UserService.ServiceName" in ids  # property

        # Other type containers
        assert "Example.Test.IUserRepository" in ids
        assert "Example.Test.UserRole" in ids
        assert "Example.Test.User" in ids  # record

        # Enum members are indexed as constants parented under the enum.
        assert "Example.Test.UserRole.Admin" in ids
        assert "Example.Test.UserRole.User" in ids
        assert "Example.Test.UserRole.Guest" in ids
        admin_member = next(c for c in result if c["id"] == "Example.Test.UserRole.Admin")
        assert admin_member["type"] == "constant"

        # Constants vs variables
        max_users = next(c for c in result if c["id"] == "Example.Test.UserService.MAX_USERS")
        assert max_users["type"] == "constant"
        default_role = next(c for c in result if c["id"] == "Example.Test.UserService.DEFAULT_ROLE")
        assert default_role["type"] == "constant"  # static readonly
        service_name = next(c for c in result if c["id"] == "Example.Test.UserService._serviceName")
        assert service_name["type"] == "variable"

        # Visibility
        notify_admin = next(c for c in result if c["id"] == "Example.Test.UserService.NotifyAdmin")
        assert notify_admin["attributes"]["visibility"] == "private"
        add_user = next(c for c in result if c["id"] == "Example.Test.UserService.AddUser")
        assert add_user["attributes"]["visibility"] == "public"

    def test_csharp_call_graph(self, csharp_lang):
        file_path = FIXTURES_DIR / "parser_test_csharp.cs"
        _, call_graph = parse_and_analyze(file_path, CSharpParser, csharp_lang)

        # AddUser calls NotifyAdmin and LogActivity (intra-class methods).
        assert "Example.Test.UserService.AddUser" in call_graph
        add_calls = call_graph["Example.Test.UserService.AddUser"]
        assert "Example.Test.UserService.NotifyAdmin" in add_calls
        assert "Example.Test.UserService.LogActivity" in add_calls
        assert "Example.Test.UserService.ValidateUser" in add_calls
        # Cross-class static call.
        assert "Example.Test.StringHelper.Clean" in add_calls

        # ProcessUser uses local-variable type inference: the field
        # `userService` is a `UserService`, so `userService.AddUser(...)`
        # resolves through the C# flow-support helper.
        assert "Example.Test.UserProcessor.ProcessUser" in call_graph
        process_calls = call_graph["Example.Test.UserProcessor.ProcessUser"]
        assert "Example.Test.StringHelper.Clean" in process_calls


@pytest.mark.skipif(not CSHARP_AVAILABLE, reason="tree_sitter_c_sharp not installed")
class TestCSharpRegression:
    """C# regression test: round-trip through KB + mock S3."""

    def _analyze_and_export(self, space_id: str) -> KnowledgeBase:
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id=space_id, id_scheme=IdScheme.V1_STEM)
        manager = KnowledgeBaseManager()
        manager.export_knowledge_base(kb)
        return kb

    def test_save_csharp_snapshot(self):
        self._analyze_and_export("fixtures-csharp")

    def test_csharp_regression(self):
        kb_current = self._analyze_and_export("fixtures-csharp")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-csharp")
        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"


class TestJavaScriptIntegration:
    """Test JavaScript parser + call graph integration."""

    @pytest.fixture
    def javascript_lang(self):
        return Language(tsjavascript.language())

    def test_javascript_components(self, javascript_lang):
        file_path = FIXTURES_DIR / "parser_test_javascript.js"
        components, _ = parse_and_analyze(file_path, JavaScriptParser, javascript_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        # Constants and variables
        assert "parser_test_javascript.MAX_RETRIES" in ids
        assert "parser_test_javascript.DEFAULT_TIMEOUT" in ids
        assert "parser_test_javascript.globalCounter" in ids

        # Function declarations
        assert "parser_test_javascript.helperFunction" in ids
        assert "parser_test_javascript.anotherHelper" in ids
        assert "parser_test_javascript.mainFunction" in ids
        assert "parser_test_javascript.withDefaults" in ids
        assert "parser_test_javascript.withRest" in ids
        assert "parser_test_javascript.withDestructured" in ids

        # Private function (underscore prefix — should be extracted with PRIVATE visibility)
        assert "parser_test_javascript._privateHelper" in ids

        # Arrow functions
        assert "parser_test_javascript.validateInput" in ids
        assert "parser_test_javascript.fetchData" in ids

        # Async functions
        assert "parser_test_javascript.loadUser" in ids
        assert "parser_test_javascript.saveUser" in ids

        # Generator functions
        assert "parser_test_javascript.idGenerator" in ids
        assert "parser_test_javascript.fibonacci" in ids
        assert "parser_test_javascript.streamEvents" in ids

        # Nested functions
        assert "parser_test_javascript.outerFunction" in ids
        assert "parser_test_javascript.outerFunction.innerFunction" in ids
        assert "parser_test_javascript.outerFunction.anotherInner" in ids

        # ES6 class with members
        assert "parser_test_javascript.Calculator" in ids
        assert "parser_test_javascript.Calculator.constructor" in ids
        assert "parser_test_javascript.Calculator.add" in ids
        assert "parser_test_javascript.Calculator.multiply" in ids
        assert "parser_test_javascript.Calculator.calculate" in ids
        assert "parser_test_javascript.Calculator.cacheSize" in ids
        assert "parser_test_javascript.Calculator.maxPrecision" in ids
        assert "parser_test_javascript.Calculator.create" in ids
        assert "parser_test_javascript.Calculator.fetchRate" in ids

        # Private method
        assert "parser_test_javascript.Calculator.#validate" in ids

        # Class fields
        assert "parser_test_javascript.Calculator.precision" in ids
        assert "parser_test_javascript.Calculator.instanceCount" in ids

        # Inheritance
        assert "parser_test_javascript.ScientificCalculator" in ids
        assert "parser_test_javascript.ScientificCalculator.power" in ids

        # Class expression
        assert "parser_test_javascript.EventEmitter" in ids
        assert "parser_test_javascript.EventEmitter.on" in ids
        assert "parser_test_javascript.EventEmitter.emit" in ids

        # Legacy prototype class
        assert "parser_test_javascript.Animal" in ids
        assert "parser_test_javascript.Animal.speak" in ids
        assert "parser_test_javascript.Animal.toString" in ids
        assert "parser_test_javascript.Animal.create" in ids

        # Object.assign prototype
        assert "parser_test_javascript.Vehicle" in ids
        assert "parser_test_javascript.Vehicle.start" in ids
        assert "parser_test_javascript.Vehicle.stop" in ids
        assert "parser_test_javascript.Vehicle.getInfo" in ids

        # Direct prototype assignment
        assert "parser_test_javascript.Shape" in ids
        assert "parser_test_javascript.Shape.draw" in ids
        assert "parser_test_javascript.Shape.resize" in ids

        # Object literal methods
        assert "parser_test_javascript.utils.formatDate" in ids
        assert "parser_test_javascript.utils.parseDate" in ids
        assert "parser_test_javascript.utils.fetchConfig" in ids

        # Exports
        assert "parser_test_javascript.exportedFunction" in ids
        assert "parser_test_javascript.ExportedClass" in ids
        assert "parser_test_javascript.ExportedClass.getValue" in ids
        assert "parser_test_javascript.exportedArrow" in ids
        assert "parser_test_javascript.defaultExport" in ids

        # React patterns
        assert "parser_test_javascript.UserProfile" in ids
        assert "parser_test_javascript.MemoizedList" in ids
        assert "parser_test_javascript.ForwardedInput" in ids

        # DataProcessor
        assert "parser_test_javascript.DataProcessor" in ids
        assert "parser_test_javascript.DataProcessor.process" in ids
        assert "parser_test_javascript.DataProcessor.processAsync" in ids

    def test_javascript_call_graph(self, javascript_lang):
        file_path = FIXTURES_DIR / "parser_test_javascript.js"
        _, call_graph = parse_and_analyze(file_path, JavaScriptParser, javascript_lang)

        # mainFunction calls helperFunction and anotherHelper
        assert "parser_test_javascript.mainFunction" in call_graph
        calls = call_graph["parser_test_javascript.mainFunction"]
        assert "parser_test_javascript.helperFunction" in calls
        assert "parser_test_javascript.anotherHelper" in calls

        # Nested function calls
        assert "parser_test_javascript.outerFunction.anotherInner" in call_graph
        nested_calls = call_graph["parser_test_javascript.outerFunction.anotherInner"]
        assert "parser_test_javascript.outerFunction.innerFunction" in nested_calls

        # Cross-class calls
        assert "parser_test_javascript.DataProcessor.process" in call_graph
        dp_calls = call_graph["parser_test_javascript.DataProcessor.process"]
        assert "parser_test_javascript._privateHelper" in dp_calls
        assert "parser_test_javascript.validateInput" in dp_calls

        # new expression resolved
        assert "parser_test_javascript.processData" in call_graph
        pd_calls = call_graph["parser_test_javascript.processData"]
        assert "parser_test_javascript.Calculator" in pd_calls

    def test_javascript_visibility(self, javascript_lang):
        file_path = FIXTURES_DIR / "parser_test_javascript.js"
        components, _ = parse_and_analyze(file_path, JavaScriptParser, javascript_lang)

        result = [component_to_dict(c) for c in components]

        # _privateHelper function declaration should be private
        private_func = next(c for c in result if c["id"] == "parser_test_javascript._privateHelper")
        assert private_func["attributes"]["visibility"] == "private"

        # Calculator.#validate should be private
        private_method = next(
            c for c in result if c["id"] == "parser_test_javascript.Calculator.#validate"
        )
        assert private_method["attributes"]["visibility"] == "private"

    def test_javascript_parameters(self, javascript_lang):
        file_path = FIXTURES_DIR / "parser_test_javascript.js"
        components, _ = parse_and_analyze(file_path, JavaScriptParser, javascript_lang)

        result = [component_to_dict(c) for c in components]

        # withDefaults has 3 params, all optional (default values)
        with_defaults = next(c for c in result if c["id"] == "parser_test_javascript.withDefaults")
        assert len(with_defaults["attributes"]["parameters"]) == 3
        assert all(not p["required"] for p in with_defaults["attributes"]["parameters"])

        # withRest has 2 params: first (required), ...rest (optional)
        with_rest = next(c for c in result if c["id"] == "parser_test_javascript.withRest")
        assert len(with_rest["attributes"]["parameters"]) == 2
        assert with_rest["attributes"]["parameters"][0]["required"] is True
        assert with_rest["attributes"]["parameters"][1]["required"] is False


class TestJavaScriptRegression:
    """JavaScript regression test: round-trip through KB + mock S3."""

    def _analyze_and_export(self, space_id: str) -> KnowledgeBase:
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id=space_id, id_scheme=IdScheme.V1_STEM)
        manager = KnowledgeBaseManager()
        manager.export_knowledge_base(kb)
        return kb

    def test_save_javascript_snapshot(self):
        self._analyze_and_export("fixtures-javascript")

    def test_javascript_regression(self):
        kb_current = self._analyze_and_export("fixtures-javascript")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-javascript")
        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"


@pytest.mark.skipif(not RUST_AVAILABLE, reason="tree_sitter_rust not installed")
class TestRustIntegration:
    """Test Rust parser + call graph integration."""

    @pytest.fixture
    def rust_lang(self):
        return Language(tsrust.language())

    def test_rust_components(self, rust_lang):
        file_path = FIXTURES_DIR / "parser_test_rust.rs"
        components, _ = parse_and_analyze(file_path, RustParser, rust_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        # Top-level constants and free functions (file-stem prefix).
        assert "parser_test_rust.MAX_USERS" in ids
        assert "parser_test_rust.DEFAULT_ROLE" in ids
        assert "parser_test_rust.GLOBAL_COUNTER" in ids
        assert "parser_test_rust.clean" in ids
        assert "parser_test_rust.is_empty" in ids

        # Struct + impl methods (impl Foo {...} attributes methods to Foo).
        assert "parser_test_rust.UserService" in ids
        assert "parser_test_rust.UserService.new" in ids
        assert "parser_test_rust.UserService.add_user" in ids
        assert "parser_test_rust.UserService.validate_user" in ids
        assert "parser_test_rust.UserService.notify_admin" in ids
        assert "parser_test_rust.UserService.version" in ids

        # Trait + signature
        assert "parser_test_rust.UserRepository" in ids
        assert "parser_test_rust.UserRepository.save" in ids
        assert "parser_test_rust.UserRepository.find_by_id" in ids

        # `impl Trait for Type` adds trait methods to the implementing type.
        assert "parser_test_rust.InMemoryRepo" in ids
        assert "parser_test_rust.InMemoryRepo.new" in ids
        assert "parser_test_rust.InMemoryRepo.save" in ids
        assert "parser_test_rust.InMemoryRepo.find_by_id" in ids

        # Enum
        assert "parser_test_rust.UserRole" in ids

        # Cross-impl processor
        assert "parser_test_rust.UserProcessor" in ids
        assert "parser_test_rust.UserProcessor.new" in ids
        assert "parser_test_rust.UserProcessor.process_user" in ids

        # Nested module
        assert "parser_test_rust.inner.Helper" in ids
        assert "parser_test_rust.inner.Helper.assist" in ids

        # Associated fn (no &self) marked static; instance method not.
        new_fn = next(c for c in result if c["id"] == "parser_test_rust.UserService.new")
        assert new_fn["attributes"]["visibility"] == "public"

        # Trait method signatures are abstract; no concrete impl in trait body.
        save_sig = next(c for c in result if c["id"] == "parser_test_rust.UserRepository.save")
        assert save_sig["type"] == "method"

        # Visibility: public free function vs default-private helper.
        clean_fn = next(c for c in result if c["id"] == "parser_test_rust.clean")
        assert clean_fn["attributes"]["visibility"] == "public"
        is_empty_fn = next(c for c in result if c["id"] == "parser_test_rust.is_empty")
        assert is_empty_fn["attributes"]["visibility"] == "private"

    def test_rust_call_graph(self, rust_lang):
        file_path = FIXTURES_DIR / "parser_test_rust.rs"
        _, call_graph = parse_and_analyze(file_path, RustParser, rust_lang)

        # add_user calls validate_user, notify_admin, log_activity (via &self)
        # and the free `clean()` helper.
        assert "parser_test_rust.UserService.add_user" in call_graph
        add_calls = call_graph["parser_test_rust.UserService.add_user"]
        assert "parser_test_rust.UserService.notify_admin" in add_calls
        assert "parser_test_rust.UserService.log_activity" in add_calls
        assert "parser_test_rust.UserService.validate_user" in add_calls
        assert "parser_test_rust.clean" in add_calls

        # UserProcessor::new uses path call `UserService::new` -> resolved.
        assert "parser_test_rust.UserProcessor.new" in call_graph
        proc_new_calls = call_graph["parser_test_rust.UserProcessor.new"]
        assert "parser_test_rust.UserService.new" in proc_new_calls


@pytest.mark.skipif(not RUST_AVAILABLE, reason="tree_sitter_rust not installed")
class TestRustRegression:
    """Rust regression test: round-trip through KB + mock S3."""

    def _analyze_and_export(self, space_id: str) -> KnowledgeBase:
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id=space_id, id_scheme=IdScheme.V1_STEM)
        manager = KnowledgeBaseManager()
        manager.export_knowledge_base(kb)
        return kb

    def test_save_rust_snapshot(self):
        self._analyze_and_export("fixtures-rust")

    def test_rust_regression(self):
        kb_current = self._analyze_and_export("fixtures-rust")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-rust")
        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"


@pytest.mark.skipif(not PHP_AVAILABLE, reason="tree_sitter_php not installed")
class TestPhpIntegration:
    """Test PHP parser + call graph integration."""

    @pytest.fixture
    def php_lang(self):
        return Language(tsphp.language_php())

    def test_php_components(self, php_lang):
        file_path = FIXTURES_DIR / "parser_test_php.php"
        components, _ = parse_and_analyze(file_path, PhpParser, php_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        # IDs are namespace-qualified (file declares `namespace App\Services;`),
        # so the dotted namespace replaces the file stem as the id prefix.
        # Top-level constants and free functions.
        assert "App.Services.MAX_USERS" in ids
        assert "App.Services.DEFAULT_ROLE" in ids
        assert "App.Services.clean_input" in ids
        assert "App.Services.is_empty" in ids

        # Interface + signature.
        assert "App.Services.UserRepository" in ids
        assert "App.Services.UserRepository.save" in ids

        # Trait + method.
        assert "App.Services.Loggable" in ids
        assert "App.Services.Loggable.log" in ids

        # Enum + cases.
        assert "App.Services.UserRole" in ids
        assert "App.Services.UserRole.Admin" in ids
        assert "App.Services.UserRole.Member" in ids

        # Class + members.
        assert "App.Services.StringHelper.format" in ids
        assert "App.Services.UserService" in ids
        assert "App.Services.UserService.MAX_RETRIES" in ids
        assert "App.Services.UserService.name" in ids
        assert "App.Services.UserService.count" in ids
        assert "App.Services.UserService.__construct" in ids  # constructor
        assert "App.Services.UserService.save" in ids
        assert "App.Services.UserService.validateUser" in ids
        assert "App.Services.UserService.notifyAdmin" in ids
        assert "App.Services.UserService.version" in ids

        # Component type checks.
        max_retries = next(c for c in result if c["id"] == "App.Services.UserService.MAX_RETRIES")
        assert max_retries["type"] == "constant"
        name_prop = next(c for c in result if c["id"] == "App.Services.UserService.name")
        assert name_prop["type"] == "variable"
        constructor = next(c for c in result if c["id"] == "App.Services.UserService.__construct")
        assert constructor["type"] == "constructor"

        # Visibility.
        notify_admin = next(c for c in result if c["id"] == "App.Services.UserService.notifyAdmin")
        assert notify_admin["attributes"]["visibility"] == "private"
        save_method = next(c for c in result if c["id"] == "App.Services.UserService.save")
        assert save_method["attributes"]["visibility"] == "public"

    def test_php_call_graph(self, php_lang):
        file_path = FIXTURES_DIR / "parser_test_php.php"
        _, call_graph = parse_and_analyze(file_path, PhpParser, php_lang)

        # Free-function call.
        assert "App.Services.is_empty" in call_graph
        assert "App.Services.clean_input" in call_graph["App.Services.is_empty"]

        # Intra-class ($this->) and cross-class static (Class::) resolution.
        assert "App.Services.UserService.save" in call_graph
        save_calls = call_graph["App.Services.UserService.save"]
        assert "App.Services.UserService.notifyAdmin" in save_calls
        assert "App.Services.UserService.validateUser" in save_calls
        assert "App.Services.StringHelper.format" in save_calls

        # Type-inferred local variable call: $service = new UserService(); $service->save().
        assert "App.Services.UserProcessor.process" in call_graph
        process_calls = call_graph["App.Services.UserProcessor.process"]
        assert "App.Services.UserService.save" in process_calls
        assert "App.Services.clean_input" in process_calls

    def test_php_namespace_qualified_ids_avoid_collisions(self, php_lang):
        """Same-named classes in different namespaces must get distinct IDs."""
        source = b"""<?php
namespace App\\Domain {
    class User {
        public function save(): void {}
    }
}
namespace App\\Http {
    class User {
        public function render(): void {}
    }
}
"""
        parser = Parser(php_lang)
        tree = parser.parse(source)
        components = PhpParser().extract_components(
            tree.root_node, source, FIXTURES_DIR / "collision.php"
        )
        ids = [c.id for c in components]

        # Both User classes survive with namespace-distinct IDs (no collision).
        assert "App.Domain.User" in ids
        assert "App.Http.User" in ids
        assert "App.Domain.User.save" in ids
        assert "App.Http.User.render" in ids

        # Braced namespaces are scoped to their own block, not leaked to siblings.
        assert "App.Http.User.save" not in ids
        assert "App.Domain.User.render" not in ids


@pytest.mark.skipif(not PHP_AVAILABLE, reason="tree_sitter_php not installed")
class TestPhpRegression:
    """PHP regression test: round-trip through KB + mock S3."""

    def _analyze_and_export(self, space_id: str) -> KnowledgeBase:
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id=space_id, id_scheme=IdScheme.V1_STEM)
        manager = KnowledgeBaseManager()
        manager.export_knowledge_base(kb)
        return kb

    def test_save_php_snapshot(self):
        self._analyze_and_export("fixtures-php")

    def test_php_regression(self):
        kb_current = self._analyze_and_export("fixtures-php")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-php")
        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"


@pytest.mark.skipif(not RUBY_AVAILABLE, reason="tree_sitter_ruby not installed")
class TestRubyIntegration:
    """Test Ruby parser + call graph integration."""

    @pytest.fixture
    def ruby_lang(self):
        return Language(tsruby.language())

    def test_ruby_components(self, ruby_lang):
        file_path = FIXTURES_DIR / "parser_test_ruby.rb"
        components, _ = parse_and_analyze(file_path, RubyParser, ruby_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]
        by_id = {c["id"]: c for c in result}

        # Top-level constants and a free function.
        assert "parser_test_ruby.MAX_RETRIES" in ids
        assert "parser_test_ruby.DEFAULT_CURRENCY" in ids
        assert "parser_test_ruby.normalize_amount" in ids
        assert by_id["parser_test_ruby.normalize_amount"]["type"] == "function"
        assert by_id["parser_test_ruby.MAX_RETRIES"]["type"] == "constant"

        # Module nesting, with constants scoped to the module.
        assert by_id["parser_test_ruby.Billing"]["type"] == "module"
        assert "parser_test_ruby.Billing.TAX_RATE" in ids

        # Classes nested in a module, and their methods.
        assert by_id["parser_test_ruby.Billing.AuditLog"]["type"] == "class"
        assert "parser_test_ruby.Billing.AuditLog.record" in ids
        assert by_id["parser_test_ruby.Billing.AuditLog.record"]["type"] == "method"

        # `initialize` is a constructor, matching every other language's parser.
        assert by_id["parser_test_ruby.Billing.PaymentGateway.initialize"]["type"] == "constructor"

        # Class variable and class-level constant.
        assert by_id["parser_test_ruby.Billing.InvoiceService.instances"]["type"] == "variable"
        assert by_id["parser_test_ruby.Billing.InvoiceService.LIMIT"]["type"] == "constant"

        # Attribute macros generate real callable accessors, including writers.
        assert "parser_test_ruby.Billing.InvoiceService.currency" in ids
        assert "parser_test_ruby.Billing.InvoiceService.currency=" in ids
        assert "parser_test_ruby.Billing.InvoiceService.clock=" in ids
        # attr_reader takes multiple symbols; each becomes its own component.
        assert "parser_test_ruby.Billing.InvoiceService.gateway" in ids
        assert "parser_test_ruby.Billing.InvoiceService.audit" in ids
        # A reader generates no setter.
        assert "parser_test_ruby.Billing.InvoiceService.gateway=" not in ids

        # Method names keep their `?` and `!` suffixes.
        assert "parser_test_ruby.Billing.InvoiceService.valid?" in ids
        assert "parser_test_ruby.Billing.InvoiceService.refund!" in ids

        # `def self.x` and `class << self` methods are both static.
        assert by_id["parser_test_ruby.Billing.InvoiceService.build_default"]["type"] == "method"
        assert "parser_test_ruby.Billing.InvoiceService.instance_count" in ids

        # `alias` defines a method under the new name.
        assert "parser_test_ruby.Billing.InvoiceService.total_for" in ids

        # Sticky visibility: `private` / `protected` apply to everything after them.
        vis = lambda cid: by_id[cid]["attributes"]["visibility"]  # noqa: E731
        assert vis("parser_test_ruby.Billing.InvoiceService.finalize") == "private"
        assert vis("parser_test_ruby.Billing.InvoiceService.audit_trail") == "protected"
        # ...and do not leak backwards to methods declared before them.
        assert vis("parser_test_ruby.Billing.InvoiceService.create_invoice") == "public"

        # Inline `private def foo` applies to that definition only.
        assert vis("parser_test_ruby.Admin.Console.secret") == "private"
        assert vis("parser_test_ruby.Admin.Console.run") == "public"

        # A qualified class name is normalised to dots, so it nests like a module.
        assert "parser_test_ruby.Admin.Report" in ids
        assert "parser_test_ruby.Admin.Report.render" in ids

        # Retroactive visibility: ``private :check_access, :verify_token`` applied
        # after the methods are defined. Must patch already-emitted components.
        assert "parser_test_ruby.Admin.Permissions" in ids
        assert vis("parser_test_ruby.Admin.Permissions.check_access") == "private"
        assert vis("parser_test_ruby.Admin.Permissions.verify_token") == "private"
        # Methods NOT listed in the retroactive call stay public.
        assert vis("parser_test_ruby.Admin.Permissions.public_info") == "public"

        # Every parameter form is captured, with its sigil preserved.
        param_names = [
            p["name"]
            for p in by_id["parser_test_ruby.Billing.InvoiceService.calculate_total"]["attributes"][
                "parameters"
            ]
        ]
        assert param_names == ["items", "*extras", "&block"]
        init_params = {
            p["name"]: p["required"]
            for p in by_id["parser_test_ruby.Billing.InvoiceService.initialize"]["attributes"][
                "parameters"
            ]
        }
        # A keyword argument with a default is optional; `**options` never required.
        assert init_params == {"currency:": False, "**options": False}

    def test_ruby_class_declarations_recorded(self, ruby_lang):
        """Mixins and framework DSL are recorded on the class, not invented as components."""
        file_path = FIXTURES_DIR / "parser_test_ruby.rb"
        components, _ = parse_and_analyze(file_path, RubyParser, ruby_lang)

        service = next(c for c in components if c.id == "parser_test_ruby.Billing.InvoiceService")
        decorators = service.attributes.decorators
        assert "inherits BaseService" in decorators
        assert "include Auditable" in decorators
        assert "has_many :line_items, dependent: :destroy" in decorators
        assert "validates :total, presence: true" in decorators

        # The DSL calls must not become components of their own.
        ids = [c.id for c in components]
        assert "parser_test_ruby.Billing.InvoiceService.has_many" not in ids
        assert "parser_test_ruby.Billing.InvoiceService.validates" not in ids

    def test_ruby_call_graph(self, ruby_lang):
        file_path = FIXTURES_DIR / "parser_test_ruby.rb"
        _, call_graph = parse_and_analyze(file_path, RubyParser, ruby_lang)

        create = call_graph["parser_test_ruby.Billing.InvoiceService.create_invoice"]
        # Bare call with arguments, within the same class.
        assert "parser_test_ruby.Billing.InvoiceService.calculate_total" in create
        # `@gateway.charge` and `@audit.record` resolve through the instance
        # variables assigned in `initialize`.
        assert "parser_test_ruby.Billing.PaymentGateway.charge" in create
        assert "parser_test_ruby.Billing.AuditLog.record" in create
        # Paren-less zero-argument call to a private method.
        assert "parser_test_ruby.Billing.InvoiceService.finalize" in create

        # Paren-less call chain continues into the protected method.
        assert (
            "parser_test_ruby.Billing.InvoiceService.audit_trail"
            in call_graph["parser_test_ruby.Billing.InvoiceService.finalize"]
        )

        # Local variable type inference: `local = PaymentGateway.new; local.authorize`.
        assert (
            "parser_test_ruby.Billing.PaymentGateway.authorize"
            in call_graph["parser_test_ruby.Billing.InvoiceService.refund!"]
        )

        # Intra-class and intra-module bare calls.
        assert (
            "parser_test_ruby.Billing.PaymentGateway.authorize"
            in call_graph["parser_test_ruby.Billing.PaymentGateway.charge"]
        )
        assert (
            "parser_test_ruby.Billing.AuditLog.persist"
            in call_graph["parser_test_ruby.Billing.AuditLog.record"]
        )

        # Inline-private method reached by a paren-less call.
        assert (
            "parser_test_ruby.Admin.Console.secret"
            in call_graph["parser_test_ruby.Admin.Console.run"]
        )

        # Call from a qualified class out to a top-level function.
        assert (
            "parser_test_ruby.normalize_amount"
            in call_graph["parser_test_ruby.Admin.Report.render"]
        )

    def test_ruby_local_reads_are_not_calls(self, ruby_lang, tmp_path):
        """A bare identifier that names a local must not resolve to a method.

        Ruby cannot distinguish a paren-less call from a variable read at the
        grammar level, so the parser treats bare identifiers in statement
        position as calls. This guards the exclusions that keep that from
        producing bogus edges — the realistic failure mode, since an
        `attr_reader :size` and a local named `size` collide readily.

        The source is written to a real file because FlowAnalyzer re-reads each
        component's file from disk when building the call graph.
        """
        source = b"""
class Widget
  attr_reader :size

  def compute
    1
  end

  def build
    size = compute
    size
  end

  def render(size)
    size
  end

  def each_size(list)
    list.each do |size|
      size
    end
  end
end
"""
        file_path = tmp_path / "widget.rb"
        file_path.write_bytes(source)
        components, call_graph = parse_and_analyze(file_path, RubyParser, ruby_lang)

        # The right-hand side of `size = compute` is a genuine paren-less call.
        assert "widget.Widget.compute" in call_graph["widget.Widget.build"]

        # Reading a local, a parameter, or a block parameter is not a call, even
        # though a method of that name exists.
        assert "widget.Widget.size" not in call_graph.get("widget.Widget.build", [])
        assert "widget.Widget.size" not in call_graph.get("widget.Widget.render", [])
        assert "widget.Widget.size" not in call_graph.get("widget.Widget.each_size", [])


@pytest.mark.skipif(not RUBY_AVAILABLE, reason="tree_sitter_ruby not installed")
class TestRubyRegression:
    """Ruby regression test: round-trip through KB + mock S3."""

    def _analyze_and_export(self, space_id: str) -> KnowledgeBase:
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id=space_id, id_scheme=IdScheme.V1_STEM)
        manager = KnowledgeBaseManager()
        manager.export_knowledge_base(kb)
        return kb

    def test_save_ruby_snapshot(self):
        self._analyze_and_export("fixtures-ruby")

    def test_ruby_regression(self):
        kb_current = self._analyze_and_export("fixtures-ruby")

        manager = KnowledgeBaseManager()
        kb_loaded = manager.import_knowledge_base(space_id="fixtures-ruby")
        current_components = [component_to_dict(c) for c in kb_current.components]
        expected_components = [component_to_dict(c) for c in kb_loaded.components]
        assert current_components == expected_components, "Parser output changed unexpectedly"

    def test_ruby_files_are_analyzed_not_skipped(self):
        """Ruby must be reported as analyzed, never as a skipped language.

        Guards the `default.yaml` half of this change: Ruby used to be listed
        under `unsupported_languages`, and leaving it there would keep `.rb`
        files in the "skipped" report even though a parser now exists. No S3
        export here — this asserts on analysis metadata only.
        """
        catalog = ComponentCatalog()
        analyzer = CodeAnalyzer(catalog)
        kb = analyzer.analyze_directory(FIXTURES_DIR, space_id="fixtures-ruby-coverage", id_scheme=IdScheme.V1_STEM)

        metadata = kb.analysis_metadata
        assert "ruby" in metadata["supported_languages"]
        assert "ruby" in metadata["languages_detected"]
        # `skipped_files` maps language name -> count, and must not mention Ruby.
        assert "ruby" not in metadata["skipped_files"]
        assert "Ruby" not in metadata["skipped_files"]
        # And Ruby components actually made it into the catalog.
        assert any(c.language == "ruby" for c in kb.components)


@pytest.mark.skipif(not RUBY_AVAILABLE, reason="tree_sitter_ruby not installed")
class TestRubyAppBoundaries:
    """Multi-application Ruby repos: IDs qualified by app root (Gemfile/gemspec).

    Rationale (see codekb/analysis/ruby_app_boundaries.py): within ONE Ruby
    application, same-named top-level classes are the same runtime constant
    (class reopening), so merging them under one ID is correct — these tests
    pin that no prefixes appear for single-app repos. Across applications in a
    monorepo they are genuinely different classes, so components from nested
    app roots get a repo-relative dotted prefix.
    """

    USER_RB = "class User\n  def save\n    1\n  end\nend\n"

    def _analyze(self, tmp_path, files):
        for rel, body in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        catalog = ComponentCatalog()
        kb = CodeAnalyzer(catalog).analyze_directory(tmp_path, space_id="apps", id_scheme=IdScheme.V1_STEM)
        return sorted(c.id.split("::", 1)[-1] for c in kb.components)

    def test_multi_app_repo_gets_distinct_ids(self, tmp_path):
        ids = self._analyze(
            tmp_path,
            {
                "Gemfile": "source 'https://rubygems.org'\n",
                "app/models/user.rb": self.USER_RB,
                "services/billing/Gemfile": "source 'https://rubygems.org'\n",
                "services/billing/app/models/user.rb": self.USER_RB,
            },
        )
        # Root app keeps stable unprefixed IDs; nested app is qualified.
        assert "user.User" in ids
        assert "user.User.save" in ids
        assert "services.billing.user.User" in ids
        assert "services.billing.user.User.save" in ids

    def test_single_app_repo_ids_unchanged_and_reopening_merges(self, tmp_path):
        ids = self._analyze(
            tmp_path,
            {
                "Gemfile": "source 'https://rubygems.org'\n",
                "app/models/user.rb": "class User\n  def save\n    1\n  end\nend\n",
                # Reopening: same class, different method, different file — one app.
                "lib/user.rb": "class User\n  def audit\n    2\n  end\nend\n",
            },
        )
        # One class, methods from BOTH files, and no path-derived prefixes.
        assert "user.User" in ids
        assert "user.User.save" in ids
        assert "user.User.audit" in ids
        assert not any(i.startswith(("app.", "lib.")) for i in ids)

    def test_no_gemfile_repo_unchanged(self, tmp_path):
        ids = self._analyze(tmp_path, {"app/models/user.rb": self.USER_RB})
        assert "user.User.save" in ids
        assert all(not i.startswith("app.") for i in ids)

    def test_gemspec_marks_app_root(self, tmp_path):
        ids = self._analyze(
            tmp_path,
            {
                "Gemfile": "source 'https://rubygems.org'\n",
                "app/models/user.rb": self.USER_RB,
                "engines/audit/audit.gemspec": (
                    "Gem::Specification.new do |s|\n  s.name = 'audit'\nend\n"
                ),
                "engines/audit/lib/user.rb": self.USER_RB,
            },
        )
        assert "user.User.save" in ids
        assert "engines.audit.user.User.save" in ids

    def test_non_ruby_components_never_prefixed(self, tmp_path):
        ids = self._analyze(
            tmp_path,
            {
                "Gemfile": "source 'https://rubygems.org'\n",
                "app/models/user.rb": self.USER_RB,
                "services/billing/Gemfile": "source 'https://rubygems.org'\n",
                "services/billing/app/models/user.rb": self.USER_RB,
                # Python file inside the nested app root: must stay unqualified —
                # the boundary rule is Ruby semantics, not a general path scheme.
                "services/billing/scripts/tool.py": "def run():\n    return 1\n",
            },
        )
        assert "tool.run" in ids
        assert "services.billing.tool.run" not in ids


@pytest.mark.skipif(not HCL_AVAILABLE, reason="tree_sitter_hcl not installed")
class TestTerraformIntegration:
    """Test Terraform (HCL) parser + dependency-graph integration.

    Terraform is declarative, so the "call graph" here is Terraform's
    dependency graph: block -> block references (var.*, local.*, data.*,
    module.*, resource addresses). Component IDs are Terraform addresses.
    """

    @pytest.fixture
    def terraform_lang(self):
        return Language(tshcl.language())

    def test_terraform_components(self, terraform_lang):
        file_path = FIXTURES_DIR / "parser_test_terraform.tf"
        components, _ = parse_and_analyze(file_path, TerraformParser, terraform_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        # Variables (inputs) — typed as VARIABLE, addressed as var.NAME.
        assert "var.region" in ids
        assert "var.bucket_prefix" in ids

        # Each locals entry is its own component.
        assert "local.bucket_name" in ids
        assert "local.common_tags" in ids

        # Data source: data.TYPE.NAME.
        assert "data.aws_ami.ubuntu" in ids

        # Managed resources: TYPE.NAME.
        assert "aws_s3_bucket.logs" in ids
        assert "aws_instance.web" in ids

        # Module and output.
        assert "module.vpc" in ids
        assert "output.instance_ip" in ids

        # var blocks are inputs (VARIABLE); referencing blocks are FUNCTION so
        # the flow analyzer scans them for outgoing dependency edges.
        region = next(c for c in result if c["id"] == "var.region")
        assert region["type"] == "variable"
        bucket = next(c for c in result if c["id"] == "aws_s3_bucket.logs")
        assert bucket["type"] == "function"
        local_name = next(c for c in result if c["id"] == "local.bucket_name")
        assert local_name["type"] == "function"

    def test_terraform_dependency_graph(self, terraform_lang):
        file_path = FIXTURES_DIR / "parser_test_terraform.tf"
        _, call_graph = parse_and_analyze(file_path, TerraformParser, terraform_lang)

        # aws_s3_bucket.logs depends on local.bucket_name and var.region.
        assert "aws_s3_bucket.logs" in call_graph
        bucket_deps = call_graph["aws_s3_bucket.logs"]
        assert "local.bucket_name" in bucket_deps
        assert "var.region" in bucket_deps
        assert "local.common_tags" in bucket_deps

        # aws_instance.web depends on the data source, module, and resource,
        # with reference addresses truncated to the target block (attributes
        # like .id/.arn/.subnet_ids dropped). The nested `ingress { }` block's
        # var.region reference is attributed to the enclosing resource.
        assert "aws_instance.web" in call_graph
        web_deps = call_graph["aws_instance.web"]
        assert "data.aws_ami.ubuntu" in web_deps
        assert "module.vpc" in web_deps
        assert "aws_s3_bucket.logs" in web_deps
        assert "var.region" in web_deps

        # locals reference the input variables.
        assert "local.bucket_name" in call_graph
        assert "var.region" in call_graph["local.bucket_name"]
        assert "var.bucket_prefix" in call_graph["local.bucket_name"]

        # output references a resource.
        assert "output.instance_ip" in call_graph
        assert "aws_instance.web" in call_graph["output.instance_ip"]

        # module references an input variable.
        assert "module.vpc" in call_graph
        assert "var.region" in call_graph["module.vpc"]


@pytest.mark.skipif(not CPP_AVAILABLE, reason="tree_sitter_cpp not installed")
class TestCppIntegration:
    """Test C++ parser + call graph integration."""

    @pytest.fixture
    def cpp_lang(self):
        return Language(tscpp.language())

    def test_cpp_components(self, cpp_lang):
        file_path = FIXTURES_DIR / "parser_test_cpp.cpp"
        components, _ = parse_and_analyze(file_path, CppParser, cpp_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        # File-scope globals (stem-prefixed) vs namespaced members.
        assert "parser_test_cpp.global_counter" in ids
        assert "parser_test_cpp.PI" in ids

        # .cpp components are labeled language="cpp".
        assert {c.language for c in components} == {"cpp"}

        # Namespaced types thread the namespace into the id (stem is replaced).
        assert "app.Repository" in ids
        assert "app.UserService" in ids
        assert "app.Box" in ids
        assert "app.Role" in ids

        # Free function living in the namespace.
        assert "app.freeHelper" in ids

        # Class members (declared in-class).
        assert "app.UserService.UserService" in ids  # constructor
        assert "app.UserService.~UserService" in ids  # destructor
        assert "app.UserService.save" in ids
        assert "app.UserService.addUser" in ids
        assert "app.UserService.version" in ids
        assert "app.UserService.operator=" in ids
        assert "app.UserService.notifyAdmin" in ids
        assert "app.UserService.helperCount" in ids
        assert "app.UserService.count_" in ids
        assert "app.UserService.MAX_USERS" in ids
        assert "app.Box.get" in ids
        assert "app.Box.set" in ids

        # --- component type / attribute correctness ---
        def get(cid):
            return next(c for c in result if c["id"] == cid)

        assert get("parser_test_cpp.global_counter")["type"] == "variable"
        assert get("parser_test_cpp.PI")["type"] == "constant"
        assert get("app.Repository")["type"] == "class"
        assert get("app.Role")["type"] == "enum"
        assert get("app.freeHelper")["type"] == "function"
        assert get("app.UserService.UserService")["type"] == "constructor"
        assert get("app.UserService.save")["type"] == "method"
        assert get("app.UserService.count_")["type"] == "variable"
        assert get("app.UserService.MAX_USERS")["type"] == "constant"

        # Section-based access specifiers apply to every following member.
        assert get("app.UserService.save")["attributes"]["visibility"] == "public"
        assert get("app.UserService.notifyAdmin")["attributes"]["visibility"] == "protected"
        assert get("app.UserService.helperCount")["attributes"]["visibility"] == "private"
        assert get("app.UserService.count_")["attributes"]["visibility"] == "private"

        # Out-of-line definitions are parented to the owning type, not treated
        # as free functions.
        save_impl = next(c for c in components if c.id == "app.UserService.save")
        assert save_impl.parent_component == "app.UserService"
        assert save_impl.type.value == "method"

    def test_cpp_attributes(self, cpp_lang):
        file_path = FIXTURES_DIR / "parser_test_cpp.cpp"
        components, _ = parse_and_analyze(file_path, CppParser, cpp_lang)
        by_id = {c.id: c for c in components}

        # Pure virtual (`= 0`) => abstract.
        assert by_id["app.Repository.save"].attributes.is_abstract is True
        # `static` preserved even though the out-of-line definition can't restate it.
        assert by_id["app.UserService.version"].attributes.is_static is True
        # const static member => constant.
        assert by_id["app.UserService.MAX_USERS"].attributes.is_static is True

        # Parameters (from the out-of-line definition; addUser(name, age)).
        add_user = by_id["app.UserService.addUser"]
        param_names = [p.name for p in add_user.attributes.parameters]
        assert param_names == ["name", "age"]

    def test_cpp_call_graph(self, cpp_lang):
        file_path = FIXTURES_DIR / "parser_test_cpp.cpp"
        _, call_graph = parse_and_analyze(file_path, CppParser, cpp_lang)

        # Member call `notifyAdmin()` inside save().
        assert "app.UserService.save" in call_graph
        assert "app.UserService.notifyAdmin" in call_graph["app.UserService.save"]

        # notifyAdmin() calls the static version().
        assert "app.UserService.notifyAdmin" in call_graph
        assert "app.UserService.version" in call_graph["app.UserService.notifyAdmin"]

        # addUser resolves: constructor (new UserService), svc.save (local type),
        # repo->save (Repository* pointer type), and the free helper.
        assert "app.UserService.addUser" in call_graph
        add_calls = call_graph["app.UserService.addUser"]
        assert "app.UserService.save" in add_calls
        assert "app.Repository.save" in add_calls
        assert "app.freeHelper" in add_calls


@pytest.mark.skipif(not C_AVAILABLE, reason="tree_sitter_c not installed")
class TestCIntegration:
    """Test C parser + call graph integration (shares the C/C++ parser)."""

    @pytest.fixture
    def c_lang(self):
        return Language(tsc.language())

    def test_c_components(self, c_lang):
        file_path = FIXTURES_DIR / "parser_test_c.c"
        components, _ = parse_and_analyze(file_path, CppParser, c_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]

        assert "parser_test_c.g_count" in ids
        assert "parser_test_c.MAX_ITEMS" in ids
        assert "parser_test_c.Point" in ids

        # C components must be labeled language="c" (mirrors the .c grammar
        # routing), not the shared parser's historical hardcoded "cpp".
        assert {c.language for c in components} == {"c"}
        assert "parser_test_c.Point.x" in ids
        assert "parser_test_c.Point.y" in ids
        assert "parser_test_c.add" in ids
        assert "parser_test_c.compute" in ids
        assert "parser_test_c.run" in ids

        # typedef'd anonymous struct is captured under the typedef name.
        assert "parser_test_c.Coord" in ids
        assert "parser_test_c.Coord.lat" in ids
        assert "parser_test_c.Coord.lng" in ids

        def get(cid):
            return next(c for c in result if c["id"] == cid)

        assert get("parser_test_c.g_count")["type"] == "variable"
        assert get("parser_test_c.MAX_ITEMS")["type"] == "constant"
        assert get("parser_test_c.Point")["type"] == "class"
        assert get("parser_test_c.Coord")["type"] == "class"
        assert get("parser_test_c.add")["type"] == "function"
        # A function-pointer variable must NOT be misread as a function.
        assert get("parser_test_c.g_comparator")["type"] == "variable"
        # Prototype + definition of add() collapse into a single component.
        assert ids.count("parser_test_c.add") == 1
        add_comp = next(c for c in components if c.id == "parser_test_c.add")
        assert [p.name for p in add_comp.attributes.parameters] == ["a", "b"]

    def test_c_call_graph(self, c_lang):
        file_path = FIXTURES_DIR / "parser_test_c.c"
        _, call_graph = parse_and_analyze(file_path, CppParser, c_lang)

        assert "parser_test_c.compute" in call_graph
        assert "parser_test_c.add" in call_graph["parser_test_c.compute"]
        assert "parser_test_c.run" in call_graph
        assert "parser_test_c.compute" in call_graph["parser_test_c.run"]


class TestCppCatalogDedup:
    """C/C++ header/source dedup is order-independent, without changing other
    languages' catalog behavior."""

    @staticmethod
    def _mk(cid: str, language: str, lines: int):
        from src.core.models import ComponentAttributes, ComponentType

        return Component(
            id=cid,
            type=ComponentType.METHOD,
            file_path=Path(f"{cid}.stub"),
            line_number=1,
            end_line_number=lines,
            language=language,
            attributes=ComponentAttributes(line_count=lines),
        )

    @staticmethod
    def _find(cat, cid: str):
        return next((c for c in cat.get_all_components() if c.id == cid), None)

    def test_cpp_definition_wins_regardless_of_order(self):
        proto = self._mk("app.Foo.bar", "cpp", 1)  # header prototype
        definition = self._mk("app.Foo.bar", "cpp", 12)  # source definition

        # definition added first, then prototype
        cat1 = ComponentCatalog()
        cat1.add_component(definition)
        cat1.add_component(proto)
        assert self._find(cat1, "app.Foo.bar").attributes.line_count == 12

        # prototype added first, then definition
        cat2 = ComponentCatalog()
        cat2.add_component(proto)
        cat2.add_component(definition)
        assert self._find(cat2, "app.Foo.bar").attributes.line_count == 12

    def test_c_definition_wins_regardless_of_order(self):
        proto = self._mk("util.add", "c", 1)
        definition = self._mk("util.add", "c", 5)

        cat = ComponentCatalog()
        cat.add_component(proto)
        cat.add_component(definition)
        assert self._find(cat, "util.add").attributes.line_count == 5

    def test_other_languages_keep_last_wins(self):
        """Non-C/C++ collisions must still overwrite (unchanged behavior)."""
        first = self._mk("mod.func", "python", 10)
        second = self._mk("mod.func", "python", 2)  # smaller, but must still win

        cat = ComponentCatalog()
        cat.add_component(first)
        cat.add_component(second)
        # Last-wins preserved for other languages even though it's smaller.
        assert self._find(cat, "mod.func").attributes.line_count == 2


@pytest.mark.skipif(not CPP_AVAILABLE, reason="tree_sitter_cpp not installed")
class TestCppMacroHandling:
    """Declaration-position macro robustness (Findings 1 & 2).

    tree-sitter cannot expand C macros; the wrapper's length-preserving
    pre-clean neutralizes them so classes/members are recovered and no
    spurious components are emitted. These tests exercise that pre-clean
    directly (the wrapper applies it before parsing c/cpp).
    """

    @pytest.fixture
    def cpp_lang(self):
        return Language(tscpp.language())

    def _ids(self, source: str, cpp_lang):
        from src.core.tree_sitter_wrapper import _sanitize_c_family_source

        src = _sanitize_c_family_source(source.encode())
        tree = Parser(cpp_lang).parse(src)
        parser = CppParser()
        parser._scheme = IdScheme.V1_STEM  # assert readable, stable V1 ids
        comps = parser.extract_components(tree.root_node, src, Path("m.cpp"))
        return comps

    def test_export_macro_class_recovered(self, cpp_lang):
        # Finding 1: `class LEVELDB_EXPORT Iterator` — class + pure-virtuals.
        src = (
            "namespace leveldb {\n"
            "class LEVELDB_EXPORT Iterator {\n"
            " public:\n"
            "  virtual bool Valid() const = 0;\n"
            "  virtual void Seek(const Slice& t) = 0;\n"
            "};\n"
            "}\n"
        )
        comps = self._ids(src, cpp_lang)
        ids = [c.id for c in comps]
        assert "leveldb.Iterator" in ids  # class recovered
        assert "leveldb.Iterator.Valid" in ids
        assert "leveldb.Iterator.Seek" in ids
        # the macro token must not become a component
        assert not any("LEVELDB_EXPORT" in i for i in ids)
        # pure virtuals are abstract
        by_id = {c.id: c for c in comps}
        assert by_id["leveldb.Iterator.Valid"].attributes.is_abstract is True

    def test_class_position_annotation_macro_recovered(self, cpp_lang):
        # Finding 1: `class SCOPED_LOCKABLE MutexLock`.
        src = "class SCOPED_LOCKABLE MutexLock {\n public:\n  void Lock();\n};\n"
        ids = [c.id for c in self._ids(src, cpp_lang)]
        assert "m.MutexLock" in ids
        assert "m.MutexLock.Lock" in ids
        assert not any("SCOPED_LOCKABLE" in i for i in ids)

    def test_trailing_annotation_macros_no_spurious_components(self, cpp_lang):
        # Finding 2: GUARDED_BY / LOCKS_EXCLUDED must not create fake methods
        # nor drop the real member.
        src = (
            "namespace leveldb {\n"
            "class Foo {\n"
            " public:\n"
            "  bool Insert(const std::string& f) LOCKS_EXCLUDED(mu_) { return true; }\n"
            " private:\n"
            "  int locked_ GUARDED_BY(mu_);\n"
            "  int plain_;\n"
            "};\n"
            "}\n"
        )
        comps = self._ids(src, cpp_lang)
        ids = [c.id for c in comps]
        assert "leveldb.Foo.Insert" in ids
        assert "leveldb.Foo.locked_" in ids  # real member recovered
        assert "leveldb.Foo.plain_" in ids
        # no bogus components named after the annotation macros
        assert not any(i.endswith(".GUARDED_BY") for i in ids)
        assert not any(i.endswith(".LOCKS_EXCLUDED") for i in ids)
        by_id = {c.id: c for c in comps}
        assert by_id["leveldb.Foo.locked_"].type.value == "variable"

    def test_macro_statements_in_body_are_not_stripped(self, cpp_lang):
        # Safety: curated trailing-annotation set means macro *statements* in a
        # body (e.g. a CHECK) are left alone — the function still parses and its
        # call is still visible.
        src = "namespace leveldb {\nvoid run() {\n  CHECK(ready());\n  helper();\n}\n}\n"
        ids = [c.id for c in self._ids(src, cpp_lang)]
        assert "leveldb.run" in ids

    def test_class_macro_does_not_misfire_on_elaborated_type_variable(self):
        # The class-macro pre-clean must only fire on a genuine class-definition
        # head (name followed by `{` / `:` / `final`), NOT on an elaborated-type
        # variable declaration like `struct GLOBAL_STATE state;` where
        # GLOBAL_STATE is a real all-caps struct tag (common in C).
        from src.core.tree_sitter_wrapper import _sanitize_c_family_source as sanitize

        # Must be left byte-for-byte unchanged (tag not blanked).
        for decl in (
            b"struct GLOBAL_STATE state;",
            b"struct GLOBAL_STATE state = init();",
            b"struct GLOBAL_STATE *p;",  # pointer form
            b"struct GLOBAL_STATE arr[10];",
        ):
            assert sanitize(decl) == decl, decl

        # Real class-definition heads must still be sanitized (macro blanked).
        for head in (
            b"class LEVELDB_EXPORT Iterator {",
            b"struct SCOPED_LOCKABLE MutexLock {",
            b"class LEVELDB_EXPORT DB : public Base {",
            b"class LEVELDB_EXPORT Foo final {",
        ):
            out = sanitize(head)
            assert b"LEVELDB_EXPORT" not in out and b"SCOPED_LOCKABLE" not in out, head
            assert len(out) == len(head), head  # length-preserving


class TestDeepNestingDoSGuard:
    """Regression tests for unbounded syntax-tree recursion on untrusted input.

    Ingested repositories are untrusted, so a pathologically deep source file
    must not exhaust the interpreter stack during component extraction or node
    counting (a denial-of-service on ingest).
    """

    class _FakeNode:
        """Minimal stand-in for a tree-sitter node exposing only ``children``."""

        def __init__(self, children=()):
            self.children = list(children)

    @classmethod
    def _linear_chain(cls, length: int) -> "TestDeepNestingDoSGuard._FakeNode":
        """Build a single-child chain ``length`` levels deep."""
        node = cls._FakeNode()
        for _ in range(length):
            node = cls._FakeNode([node])
        return node

    def test_deeply_nested_file_is_skipped_not_crashed(self, tmp_path):
        """A deeply nested file is skipped by the central guard, not crashed.

        Exercises the real protection: extraction routes through
        ``TreeSitterWrapper.parse_file`` for every language, so a tree deeper
        than the ceiling is skipped there rather than recursing into any parser.
        """
        from src.core.config import get_positive_int
        from src.core.tree_sitter_wrapper import TreeSitterWrapper

        depth = get_positive_int("analysis.max_ast_depth") * 4
        source = "x = " + "(" * depth + "1" + ")" * depth + "\n"
        deep_file = tmp_path / "deep.py"
        deep_file.write_text(source)

        result = TreeSitterWrapper().parse_file(deep_file, "python")

        assert result.components == []
        assert any("maximum depth" in err for err in result.parse_errors)

    def test_tree_depth_helpers_are_iterative(self):
        """Depth check and node count survive a tree deeper than the stack."""
        from src.core.config import get_positive_int
        from src.core.tree_sitter_wrapper import TreeSitterWrapper

        limit = get_positive_int("analysis.max_ast_depth")

        # Far deeper than Python's recursion limit: a recursive implementation
        # of either helper would raise RecursionError here.
        node = self._linear_chain(10_000)

        # Helpers ignore ``self``; call unbound to avoid full wrapper init.
        assert TreeSitterWrapper._tree_exceeds_depth(None, node, limit) is True
        assert TreeSitterWrapper._count_nodes(None, node) == 10_001

    def test_shallow_tree_not_flagged_as_too_deep(self):
        """A normal-depth tree is neither skipped nor miscounted."""
        from src.core.config import get_positive_int
        from src.core.tree_sitter_wrapper import TreeSitterWrapper

        limit = get_positive_int("analysis.max_ast_depth")
        node = self._linear_chain(10)

        assert TreeSitterWrapper._tree_exceeds_depth(None, node, limit) is False
        assert TreeSitterWrapper._count_nodes(None, node) == 11


# Expected shape of every component the Dart fixture declares: id suffix, kind,
# visibility and line range. Written out rather than derived so a drift in any
# one declaration fails loudly.
DART_U1_EXPECTED_COMPONENTS = [
    # Top-level variables and constants.
    ("maxRetries", "constant", "public", 16, 16),
    ("requestCount", "variable", "public", 19, 19),
    ("_internalCounter", "variable", "private", 22, 22),
    ("deferredLabel", "variable", "public", 25, 25),
    # Top-level functions, including a private one.
    ("computeTotal", "function", "public", 29, 31),
    ("describe", "function", "public", 34, 36),
    ("_reset", "function", "private", 39, 42),
    # Top-level accessors; getters and setters both map to `method`.
    ("retryBudget.get", "method", "public", 45, 45),
    ("retryBudget.set", "method", "public", 48, 50),
    ("splitLabel", "function", "public", 53, 56),
    ("classify", "function", "public", 59, 66),
    # Abstract, sealed, final and base classes.
    ("Shape", "class", "public", 69, 72),
    ("Shape.area", "method", "public", 71, 71),
    ("Result", "class", "public", 75, 75),
    ("Success", "class", "public", 78, 84),
    ("Success.new", "constructor", "public", 83, 83),
    ("Failure", "class", "public", 87, 90),
    ("Failure.new", "constructor", "public", 89, 89),
    # The member-heavy class.
    ("Repository", "class", "public", 93, 154),
    ("Repository.new", "constructor", "public", 101, 101),
    ("Repository.seeded", "constructor", "public", 105, 107),
    ("Repository.empty", "constructor", "public", 110, 110),
    ("Repository.merge", "method", "public", 113, 115),
    ("Repository.version.get", "method", "public", 118, 118),
    ("Repository.version.set", "method", "public", 121, 123),
    ("Repository.operator[]:1", "method", "public", 126, 126),
    ("Repository.operator[]=:2", "method", "public", 129, 131),
    ("Repository.operator-:0", "method", "public", 134, 134),
    ("Repository.operator-:1", "method", "public", 138, 138),
    ("Repository.of", "method", "public", 141, 141),
    ("Repository._bump", "method", "private", 144, 146),
    ("Repository.build", "method", "public", 150, 153),
    ("Widget", "class", "public", 157, 163),
    ("Widget.build", "method", "public", 160, 162),
    # Plain enum with a private value.
    ("Status", "enum", "public", 166, 170),
    ("Status.pending", "constant", "public", 167, 167),
    ("Status.active", "constant", "public", 168, 168),
    ("Status._retired", "constant", "private", 169, 169),
    # Enhanced enum: values, its own constructor, and a getter.
    ("Priority", "enum", "public", 173, 182),
    ("Priority.low", "constant", "public", 174, 174),
    ("Priority.high", "constant", "public", 175, 175),
    ("Priority.new", "constructor", "public", 179, 179),
    ("Priority.isUrgent.get", "method", "public", 181, 181),
]

# The appended constructs, in fixture order. Every one maps onto a kind the
# parser already produced.
DART_U2_EXPECTED_COMPONENTS = [
    # A mixin is a `class`; its members carry the mixin's name, with the
    # accessor suffixes a class pair carries.
    ("Auditable", "class", "public", 192, 209),
    ("Auditable.record", "method", "public", 196, 198),
    ("Auditable.entryCount.get", "method", "public", 201, 201),
    ("Auditable.entryCount.set", "method", "public", 204, 208),
    # `mixin class` is one class, not a second mixin component.
    ("Loggable", "class", "public", 213, 218),
    ("Loggable.log", "method", "public", 215, 217),
    # A named extension is a `class`; the extended type is in its signature and
    # never in its identity.
    ("RepositoryReport", "class", "public", 222, 237),
    ("RepositoryReport.summarise", "method", "public", 226, 228),
    ("RepositoryReport.tally.get", "method", "public", 231, 231),
    ("RepositoryReport.tally.set", "method", "public", 234, 236),
    # The collision case: a top-level function and an unnamed extension member
    # of the same name. The extension's member is qualified by the extended
    # type, so neither is lost.
    ("shout", "function", "public", 242, 244),
    ("extension$String.shout", "method", "public", 250, 252),
    ("extension$int.shout", "method", "public", 258, 260),
    # An extension type is its own construct, and its members are attributed to
    # its own declared name.
    ("Meters", "class", "public", 267, 275),
    ("Meters.raw.get", "method", "public", 269, 269),
    ("Meters.scaled", "method", "public", 272, 274),
    # Both typedef forms, plus a modern typedef whose alias is not a function
    # type.
    ("Transform", "class", "public", 278, 278),
    ("Notify", "class", "public", 281, 281),
    ("LabelList", "class", "public", 286, 286),
]

DART_EXPECTED_COMPONENTS = DART_U1_EXPECTED_COMPONENTS + DART_U2_EXPECTED_COMPONENTS


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
class TestDartIntegration:
    """Test Dart parser + call graph integration."""

    @pytest.fixture
    def dart_lang(self):
        return Language(tsdart.language())

    @staticmethod
    def _line_ranges(components: List[Component]) -> Dict[str, Any]:
        return {c.id: (c.line_number, c.end_line_number) for c in components}

    @staticmethod
    def _error_nodes(node: Any) -> List[Any]:
        found = []
        if node.type == "ERROR" or node.is_missing:
            found.append(node)
        for child in node.children:
            found.extend(TestDartIntegration._error_nodes(child))
        return found

    @staticmethod
    def _find_node(node: Any, node_type: str, text_starts_with: str) -> Any:
        """Find the first node of a type whose own text starts with a marker."""
        if node.type == node_type and node.text.decode("utf-8").startswith(text_starts_with):
            return node
        for child in node.children:
            found = TestDartIntegration._find_node(child, node_type, text_starts_with)
            if found is not None:
                return found
        return None

    def test_dart_components(self, dart_lang):
        """Every construct in the fixture yields the expected component."""
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        components, _ = parse_and_analyze(file_path, DartParser, dart_lang)

        result = [component_to_dict(c) for c in components]
        ids = [c["id"] for c in result]
        by_id = {c["id"]: c for c in result}

        # No identity is lost to a collision within one file.
        assert len(ids) == len(set(ids)), "duplicate Dart component ids"

        for suffix, kind, visibility, start, end in DART_EXPECTED_COMPONENTS:
            cid = f"parser_test_dart.{suffix}"
            assert cid in by_id, f"missing component {cid}"
            assert by_id[cid]["type"] == kind, f"{cid} kind"
            assert by_id[cid]["attributes"]["visibility"] == visibility, f"{cid} visibility"
            assert (by_id[cid]["line_number"], by_id[cid]["end_line_number"]) == (
                start,
                end,
            ), f"{cid} line range"

        # The fixture declares exactly this set: an extra component is as much a
        # defect as a missing one.
        assert len(ids) == len(DART_EXPECTED_COMPONENTS)

        # No new component kind was introduced for Dart.
        assert {c["type"] for c in result} <= {
            "class",
            "method",
            "function",
            "constructor",
            "enum",
            "variable",
            "constant",
        }

        # Structural attributes come from the tree.
        repository = next(c for c in components if c.id == "parser_test_dart.Repository.build")
        assert repository.attributes.signature == "String build(String prefix)"
        assert repository.attributes.documentation is not None
        assert "`build` method" in repository.attributes.documentation
        assert repository.parent_component == "parser_test_dart.Repository"

        # Modifiers are read, not guessed.
        assert by_id["parser_test_dart.Shape"]["type"] == "class"
        shape = next(c for c in components if c.id == "parser_test_dart.Shape")
        assert shape.attributes.is_abstract is True
        success = next(c for c in components if c.id == "parser_test_dart.Success")
        assert success.attributes.is_final is True
        static_method = next(c for c in components if c.id == "parser_test_dart.Repository.of")
        assert static_method.attributes.is_static is True

        # Call sites feed the existing two-pass call graph.
        _, call_graph = parse_and_analyze(file_path, DartParser, dart_lang)
        assert "parser_test_dart.Repository._bump" in call_graph.get(
            "parser_test_dart.Repository.build", []
        )
        assert "parser_test_dart.describe" in call_graph.get("parser_test_dart.Widget.build", [])

        # The integration boundary: the wrapper registered both the grammar
        # parser and the factory-made Dart parser.
        from src.core.tree_sitter_wrapper import TreeSitterWrapper

        wrapper = TreeSitterWrapper()
        assert "dart" in wrapper.supported_languages
        assert wrapper.supported_languages["dart"]["extensions"] == [".dart"]
        assert "dart" in wrapper.parsers
        assert isinstance(wrapper.language_parsers["dart"], DartParser)

    def test_dart_identity_collisions_stay_distinct(self, dart_lang):
        """The five possible collision cases are each two components.

        A named constructor beside a method of the same name is deliberately absent:
        Dart rejects that class, so the case cannot reach the parser.
        """
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        components, _ = parse_and_analyze(file_path, DartParser, dart_lang)
        by_id = {c.id: c for c in components}

        pairs = [
            # 1. A getter and a setter of the same name.
            ("parser_test_dart.Repository.version.get", "parser_test_dart.Repository.version.set"),
            # 2. An unnamed constructor beside its own class.
            ("parser_test_dart.Repository.new", "parser_test_dart.Repository"),
            # 3. Index read beside index write — same arity-bearing form, different token.
            ("parser_test_dart.Repository.operator[]:1", "parser_test_dart.Repository.operator[]=:2"),
            # 4. Unary minus beside binary minus on one class — same token, different arity.
            ("parser_test_dart.Repository.operator-:0", "parser_test_dart.Repository.operator-:1"),
            # 5. `build` on two classes in one file.
            ("parser_test_dart.Repository.build", "parser_test_dart.Widget.build"),
        ]

        for left, right in pairs:
            assert left in by_id, f"missing {left}"
            assert right in by_id, f"missing {right}"
            # Each side must be its own declaration in the file, not one
            # declaration reached under two keys: a scheme that collapsed the
            # pair would land both ids on the same source line.
            assert by_id[left].line_number != by_id[right].line_number, (
                f"{left} and {right} name the same declaration at line "
                f"{by_id[left].line_number}"
            )

        # The kind is never what keeps two names apart: the unnamed constructor
        # and its class differ by the `new` member name alone.
        assert by_id["parser_test_dart.Repository.new"].type == ComponentType.CONSTRUCTOR
        assert by_id["parser_test_dart.Repository"].type == ComponentType.CLASS
        assert by_id["parser_test_dart.Repository.new"].parent_component == (
            "parser_test_dart.Repository"
        )

        # No constructor carries a kind suffix, so a call site's `Class.name`
        # and `Class(...)` have something to resolve to.
        constructors = [c.id for c in components if c.type == ComponentType.CONSTRUCTOR]
        assert sorted(constructors) == [
            "parser_test_dart.Failure.new",
            "parser_test_dart.Priority.new",
            "parser_test_dart.Repository.empty",
            "parser_test_dart.Repository.new",
            "parser_test_dart.Repository.seeded",
            "parser_test_dart.Success.new",
        ]

        # Arity is read from the declaration, so the two minus operators differ
        # by their real value-parameter counts.
        assert by_id["parser_test_dart.Repository.operator-:0"].attributes.parameters == []
        assert len(by_id["parser_test_dart.Repository.operator-:1"].attributes.parameters) == 1

    def test_dart_constructor_calls_reach_their_constructor(self, dart_lang):
        """A named constructor is written ``Class.named(...)`` at the call site and the
        unnamed one ``Class(...)``, so neither may carry a kind suffix in its id or
        the call site has nothing to resolve to.
        """
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        components, call_graph = parse_and_analyze(file_path, DartParser, dart_lang)
        ids = {c.id for c in components}

        # `static Repository of(...) => Repository.seeded(seed);`
        assert "parser_test_dart.Repository.seeded" in ids
        assert "parser_test_dart.Repository.seeded" in call_graph.get(
            "parser_test_dart.Repository.of", []
        ), "a `Class.named(...)` call site must reach the named constructor"

        # `Repository operator -() => Repository();` — the caller is a method
        # because `build_call_graph` treats only FUNCTION and METHOD components
        # as callers, so a constructor body is never a caller.
        assert "parser_test_dart.Repository.new" in ids
        assert "parser_test_dart.Repository.new" in call_graph.get(
            "parser_test_dart.Repository.operator-:0", []
        ), "a `Class(...)` call site must reach the `new` constructor"

        # The edge points at the constructor, not at a same-named member: the
        # one remaining `seeded` component is the constructor.
        seeded = [c for c in components if c.get_simple_name() == "seeded"]
        assert [c.type for c in seeded] == [ComponentType.CONSTRUCTOR]

    def test_dart_enum_values_are_constants_under_their_enum(self, dart_lang):
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        components, _ = parse_and_analyze(file_path, DartParser, dart_lang)
        by_id = {c.id: c for c in components}

        for enum_id, values in [
            ("parser_test_dart.Status", ["pending", "active", "_retired"]),
            ("parser_test_dart.Priority", ["low", "high"]),
        ]:
            assert by_id[enum_id].type == ComponentType.ENUM
            assert by_id[enum_id].parent_component is None
            for value in values:
                value_id = f"{enum_id}.{value}"
                assert by_id[value_id].type == ComponentType.CONSTANT
                assert by_id[value_id].parent_component == enum_id

        # A private enum value is private by the same leading-underscore rule
        # as everything else.
        assert by_id["parser_test_dart.Status._retired"].attributes.visibility == Visibility.PRIVATE

        # An enhanced enum's own members are extracted too, under the enum.
        assert by_id["parser_test_dart.Priority.new"].type == ComponentType.CONSTRUCTOR
        assert by_id["parser_test_dart.Priority.isUrgent.get"].type == ComponentType.METHOD

    def test_dart_parameters_carry_name_type_and_style(self, dart_lang):
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        components, _ = parse_and_analyze(file_path, DartParser, dart_lang)
        by_id = {c.id: c for c in components}

        # int computeTotal(int base, [int bonus = 0, int? cap])
        positional = by_id["parser_test_dart.computeTotal"].attributes.parameters
        assert [(p.name, p.type_hint, p.style, p.required) for p in positional] == [
            ("base", "int", "positional-required", True),
            ("bonus", "int", "positional-optional", False),
            ("cap", "int?", "positional-optional", False),
        ]
        # The default is recorded as written, not inferred.
        assert positional[1].default_value == "0"
        assert positional[2].default_value is None

        # String describe({required String name, int count = 1})
        named = by_id["parser_test_dart.describe"].attributes.parameters
        assert [(p.name, p.type_hint, p.style, p.required) for p in named] == [
            ("name", "String", "named", True),
            ("count", "int", "named", False),
        ]
        assert named[1].default_value == "1"

        # A generic type annotation is kept whole, as written.
        seeded = by_id["parser_test_dart.Repository.seeded"].attributes.parameters
        assert [(p.name, p.type_hint) for p in seeded] == [("initial", "Map<String, int>")]

        # An unannotated parameter leaves the type absent rather than guessing.
        initialising = by_id["parser_test_dart.Success.new"].attributes.parameters
        assert [(p.name, p.type_hint) for p in initialising] == [("this.payload", None)]

        # The flow analyzer's contract is handed whichever node it is holding,
        # so this runs it against the real signature node from the tree rather
        # than against the component.
        source = file_path.read_bytes()
        tree = Parser(dart_lang).parse(source)
        describe_signature = self._find_node(
            tree.root_node, "function_signature", "String describe("
        )
        assert describe_signature is not None, "fixture no longer declares `describe`"
        contract = DartParser().extract_function_parameters(describe_signature, [])
        assert [(p.name, p.type_hint, p.style, p.required) for p in contract] == [
            ("name", "String", "named", True),
            ("count", "int", "named", False),
        ]

        # The same contract tolerates being handed nothing, which is the guard
        # the flow analyzer relies on.
        assert DartParser().extract_function_parameters(None, []) == []

    def test_dart_required_language_level_parses(self, dart_lang):
        """Null safety, records, patterns, and sealed, final and base classes parse."""
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        source = file_path.read_bytes()
        tree = Parser(dart_lang).parse(source)

        assert tree.root_node.has_error is False
        assert self._error_nodes(tree.root_node) == []

        # The language-level constructs are present in the fixture, so the clean
        # parse above is actually evidence about them.
        text = source.decode("utf-8")
        for feature in [
            "int? cap",  # nullable type annotation
            "late String",  # late modifier
            "required String name",  # required modifier
            "(int, String) splitLabel",  # record type
            "switch (value)",  # pattern matching
            "sealed class Result",  # sealed class
            "final class Success",  # final class
            "base class Failure",  # base class
        ]:
            assert feature in text, f"fixture no longer covers {feature}"

    def test_dart_bom_and_crlf_keep_line_ranges(self, dart_lang, tmp_path):
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        plain_bytes = file_path.read_bytes()
        baseline = self._line_ranges(parse_and_analyze(file_path, DartParser, dart_lang)[0])

        # Both copies keep the fixture's name so component ids are comparable;
        # both are derived from its bytes so there is one source of truth.
        bom_dir = tmp_path / "bom"
        bom_dir.mkdir()
        bom_path = bom_dir / "parser_test_dart.dart"
        bom_path.write_bytes(b"\xef\xbb\xbf" + plain_bytes)

        crlf_dir = tmp_path / "crlf"
        crlf_dir.mkdir()
        crlf_path = crlf_dir / "parser_test_dart.dart"
        crlf_path.write_bytes(plain_bytes.replace(b"\n", b"\r\n"))

        bom_ranges = self._line_ranges(parse_and_analyze(bom_path, DartParser, dart_lang)[0])
        crlf_ranges = self._line_ranges(parse_and_analyze(crlf_path, DartParser, dart_lang)[0])

        assert bom_ranges == baseline
        # If this ever fails, the fix is to convert CRLF to LF before parsing,
        # not to relax this assertion.
        assert crlf_ranges == baseline

    def test_dart_invalid_bytes_still_yield_components(self, dart_lang, tmp_path):
        """Invalid UTF-8 shifts no line and empties nothing; it is simply tolerated."""
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        plain_bytes = file_path.read_bytes()
        baseline = self._line_ranges(parse_and_analyze(file_path, DartParser, dart_lang)[0])

        # A lone 0xFF inside a comment is invalid UTF-8 in any position, and
        # sits where it cannot change the declarations around it.
        broken = plain_bytes.replace(b"A top-level constant", b"A top-\xfflevel constant")
        assert broken != plain_bytes
        broken_dir = tmp_path / "broken"
        broken_dir.mkdir()
        broken_path = broken_dir / "parser_test_dart.dart"
        broken_path.write_bytes(broken)

        components, _ = parse_and_analyze(broken_path, DartParser, dart_lang)

        assert components, "invalid bytes must not empty the result"
        assert self._line_ranges(components) == baseline

    def test_dart_directives_only_file_is_a_clean_parse(self, dart_lang):
        file_path = FIXTURES_DIR / "parser_test_dart_directives.dart"
        source = file_path.read_bytes()
        tree = Parser(dart_lang).parse(source)

        assert self._error_nodes(tree.root_node) == []

        components, call_graph = parse_and_analyze(file_path, DartParser, dart_lang)
        assert components == []
        assert all(not targets for targets in call_graph.values())

    # ── Extended constructs ──────────────────────────────────────────────────

    @pytest.fixture
    def dart_components(self, dart_lang):
        """The fixture's components, keyed by id."""
        file_path = FIXTURES_DIR / "parser_test_dart.dart"
        components, _ = parse_and_analyze(file_path, DartParser, dart_lang)
        return {c.id: c for c in components}

    def test_dart_mixin_is_one_class_component(self, dart_components):
        """Dart's ``mixin`` is its own declaration form; it is recorded as one
        ``class`` component carrying its declared name.
        """
        mixin = dart_components["parser_test_dart.Auditable"]
        assert mixin.type == ComponentType.CLASS
        assert mixin.parent_component is None
        assert mixin.attributes.signature == "mixin Auditable"
        assert mixin.attributes.visibility == Visibility.PUBLIC
        assert mixin.attributes.documentation is not None
        assert "A mixin with a method" in mixin.attributes.documentation

        # One component, not two: no second id names the same declaration.
        at_declaration = [
            cid
            for cid, component in dart_components.items()
            if component.line_number == mixin.line_number
        ]
        assert at_declaration == ["parser_test_dart.Auditable"]

        # The mixin's field is not a component: this parser extracts no fields.
        assert "parser_test_dart.Auditable._entries" not in dart_components

    def test_dart_mixin_member_is_attributed_to_the_mixin(self, dart_components):
        """A mixin's method is ``fileStem.MixinName.method``."""
        method = dart_components["parser_test_dart.Auditable.record"]
        assert method.type == ComponentType.METHOD
        assert method.parent_component == "parser_test_dart.Auditable"
        assert method.attributes.signature == "void record(String entry)"
        assert [p.name for p in method.attributes.parameters] == ["entry"]

        # The method is reachable only under the mixin: no top-level `record`
        # and no other enclosing name claims it.
        assert "parser_test_dart.record" not in dart_components
        assert [cid for cid in dart_components if cid.endswith(".record")] == [
            "parser_test_dart.Auditable.record"
        ]

    def test_dart_named_extension_is_one_class_with_the_extended_type_in_its_signature(
        self, dart_components
    ):
        extension = dart_components["parser_test_dart.RepositoryReport"]
        assert extension.type == ComponentType.CLASS
        assert extension.parent_component is None

        # The extended type is recorded, and recorded in the signature only.
        assert extension.attributes.signature == "extension RepositoryReport on Repository"
        assert "Repository" in extension.attributes.signature
        assert extension.id == "parser_test_dart.RepositoryReport"

        # One component for the declaration, not two.
        at_declaration = [
            cid
            for cid, component in dart_components.items()
            if component.line_number == extension.line_number
        ]
        assert at_declaration == ["parser_test_dart.RepositoryReport"]

    def test_dart_extension_member_is_attributed_to_the_extension_not_the_extended_type(
        self, dart_components
    ):
        """The extended type is one the fixture itself declares, so
        ``parser_test_dart.Repository.summarise`` is a real alternative identity
        rather than an impossible one.
        """
        assert "parser_test_dart.RepositoryReport.summarise" in dart_components
        assert (
            dart_components["parser_test_dart.RepositoryReport.summarise"].parent_component
            == "parser_test_dart.RepositoryReport"
        )

        # The extended type declares no such member, so the catalog must not
        # claim one on it.
        for absent in (
            "parser_test_dart.Repository.summarise",
            "parser_test_dart.Repository.tally.get",
            "parser_test_dart.Repository.tally.set",
        ):
            assert absent not in dart_components, (
                f"{absent} attributes a member to the extended type"
            )

        # And `Repository` itself still carries exactly the members it declares,
        # so nothing was moved off it either.
        on_repository = sorted(
            cid
            for cid, component in dart_components.items()
            if component.parent_component == "parser_test_dart.Repository"
        )
        assert on_repository == [
            "parser_test_dart.Repository._bump",
            "parser_test_dart.Repository.build",
            "parser_test_dart.Repository.empty",
            "parser_test_dart.Repository.merge",
            "parser_test_dart.Repository.new",
            "parser_test_dart.Repository.of",
            "parser_test_dart.Repository.operator-:0",
            "parser_test_dart.Repository.operator-:1",
            "parser_test_dart.Repository.operator[]:1",
            "parser_test_dart.Repository.operator[]=:2",
            "parser_test_dart.Repository.seeded",
            "parser_test_dart.Repository.version.get",
            "parser_test_dart.Repository.version.set",
        ]

    def test_dart_extension_accessor_pair_carries_the_class_suffixes(self, dart_components):
        getter = dart_components["parser_test_dart.RepositoryReport.tally.get"]
        setter = dart_components["parser_test_dart.RepositoryReport.tally.set"]

        assert getter.id != setter.id
        assert getter.line_number != setter.line_number
        assert getter.type == setter.type == ComponentType.METHOD
        assert getter.parent_component == setter.parent_component

        # The same suffix pair a class's getter and setter carry.
        class_getter = dart_components["parser_test_dart.Repository.version.get"]
        class_setter = dart_components["parser_test_dart.Repository.version.set"]
        assert getter.id.rsplit(".", 1)[1] == class_getter.id.rsplit(".", 1)[1] == "get"
        assert setter.id.rsplit(".", 1)[1] == class_setter.id.rsplit(".", 1)[1] == "set"

        # The same holds inside a mixin.
        assert "parser_test_dart.Auditable.entryCount.get" in dart_components
        assert "parser_test_dart.Auditable.entryCount.set" in dart_components

    def test_dart_unnamed_extension_member_does_not_collide_with_a_top_level_function(
        self, dart_components
    ):
        """A top-level ``shout`` and an unnamed ``String`` extension declaring
        ``shout`` are both clean Dart. The catalog is last-write-wins on identity, so
        an empty enclosing name for the extension's member would drop one of the two.
        """
        top_level = dart_components["parser_test_dart.shout"]
        extension_member = dart_components["parser_test_dart.extension$String.shout"]

        assert top_level.id != extension_member.id
        assert top_level.line_number != extension_member.line_number
        assert top_level.type == ComponentType.FUNCTION
        assert extension_member.type == ComponentType.METHOD

        # The qualifier is not a declared name: no component is named
        # `extension$String`, and the real `String` type is not claimed either.
        assert "parser_test_dart.extension$String" not in dart_components
        assert "parser_test_dart.String.shout" not in dart_components

        # The extension declares no name, so there is nothing to parent to.
        assert extension_member.parent_component is None

        # Both survive: three `shout` declarations, three components.
        shouts = sorted(cid for cid in dart_components if cid.endswith("shout"))
        assert shouts == [
            "parser_test_dart.extension$String.shout",
            "parser_test_dart.extension$int.shout",
            "parser_test_dart.shout",
        ]

    def test_dart_two_unnamed_extensions_on_different_types_stay_distinct(self, dart_components):
        on_string = dart_components["parser_test_dart.extension$String.shout"]
        on_int = dart_components["parser_test_dart.extension$int.shout"]

        assert on_string.id != on_int.id
        assert on_string.line_number != on_int.line_number
        assert on_string.attributes.signature == on_int.attributes.signature == "String shout()"

        # Two unnamed extensions on the SAME type declaring the same member
        # would still collide. That shape is ambiguous in Dart itself at the
        # call site, so it is a recorded limitation; hence two different types.
        assert on_string.id.split(".")[1] != on_int.id.split(".")[1]

    def test_dart_extension_type_members_are_attributed_to_its_own_name(self, dart_components):
        """An ``extension type`` is its own Dart declaration and not an extension, so
        its members are attributed the way a class's are rather than through the
        unnamed-extension path, which has no ``on`` clause to read.
        """
        extension_type = dart_components["parser_test_dart.Meters"]
        assert extension_type.type == ComponentType.CLASS
        assert extension_type.parent_component is None
        assert extension_type.attributes.signature == "extension type Meters (int value)"

        # Members under the extension type's own declared name.
        assert (
            dart_components["parser_test_dart.Meters.raw.get"].parent_component
            == "parser_test_dart.Meters"
        )
        assert (
            dart_components["parser_test_dart.Meters.scaled"].parent_component
            == "parser_test_dart.Meters"
        )

        # Not routed through the unnamed-extension path, which has no `on`
        # clause to read and would have produced a marker-qualified identity.
        assert not any(
            cid.startswith("parser_test_dart.extension$int.raw") for cid in dart_components
        )
        assert not any("extension$" in cid and "scaled" in cid for cid in dart_components)

        # The representation field is not a component.
        assert "parser_test_dart.Meters.value" not in dart_components

    def test_dart_both_typedef_forms_record_what_they_alias(self, dart_components):
        """Both forms are one ``class`` each. A modern typedef need not alias a
        function type — ``typedef LabelList = List<String>`` aliases a parameterised
        type — so the signature records whatever the declaration aliases.
        """
        modern = dart_components["parser_test_dart.Transform"]
        legacy = dart_components["parser_test_dart.Notify"]
        non_function = dart_components["parser_test_dart.LabelList"]

        for component in (modern, legacy, non_function):
            assert component.type == ComponentType.CLASS
            assert component.parent_component is None

        # The modern form records its right-hand side.
        assert modern.attributes.signature == "String Function(String input)"
        # The legacy form records the function signature as written.
        assert legacy.attributes.signature == "void Notify(String message)"
        # And the non-function alias is recorded as the type it actually names.
        assert non_function.attributes.signature == "List<String>"
        assert "Function" not in non_function.attributes.signature

    def test_dart_mixin_class_is_extracted_once_as_a_class(self, dart_components):
        """Dart's ``mixin class`` declares both, and must still yield one component."""
        mixin_class = dart_components["parser_test_dart.Loggable"]
        assert mixin_class.type == ComponentType.CLASS
        assert mixin_class.attributes.signature == "mixin class Loggable"

        # Once, not twice: the class path and the mixin path cannot both claim it.
        at_declaration = [
            cid
            for cid, component in dart_components.items()
            if component.line_number == mixin_class.line_number
        ]
        assert at_declaration == ["parser_test_dart.Loggable"]

        # Its member is attributed to it, as a class member.
        assert (
            dart_components["parser_test_dart.Loggable.log"].parent_component
            == "parser_test_dart.Loggable"
        )

    def test_dart_u1_identities_are_unchanged(self, dart_components):
        """Identities already in the catalog must not be re-keyed: a re-keyed
        component is lost silently, the exact failure the scheme exists to prevent.
        """
        for suffix, kind, visibility, start, end in DART_U1_EXPECTED_COMPONENTS:
            cid = f"parser_test_dart.{suffix}"
            assert cid in dart_components, f"u1 identity {cid} was lost"
            component = dart_components[cid]
            assert component.type.value == kind, f"{cid} kind changed"
            assert component.attributes.visibility.value == visibility, f"{cid} visibility changed"
            assert (component.line_number, component.end_line_number) == (start, end), (
                f"{cid} line range moved — the new content was inserted rather than appended"
            )

        # No appended identity shadows an earlier one, and none is missing.
        u1_ids = {f"parser_test_dart.{row[0]}" for row in DART_U1_EXPECTED_COMPONENTS}
        u2_ids = {f"parser_test_dart.{row[0]}" for row in DART_U2_EXPECTED_COMPONENTS}
        assert u1_ids & u2_ids == set()
        assert u1_ids <= set(dart_components)

        # Every member's parent is the parent originally recorded.
        assert (
            dart_components["parser_test_dart.Repository.build"].parent_component
            == "parser_test_dart.Repository"
        )
        assert dart_components["parser_test_dart.computeTotal"].parent_component is None


class TestDartGrammarUnavailableBranches:
    """The Dart grammar-import and grammar-initialisation failure branches, which an
    installed grammar never reaches. Both exist so a missing or broken grammar
    leaves ``.dart`` files reported as skipped rather than failing the whole run.
    Reaching them needs the wrapper's ``__new__`` singleton reset: ``_instance``
    is cached and ``_initialized`` short-circuits ``__init__``.
    """

    @pytest.fixture(autouse=True)
    def _isolated_wrapper(self):
        """Give each test its own wrapper construction, then restore the shared one."""
        from src.core import tree_sitter_wrapper as tsw

        saved_instance = tsw.TreeSitterWrapper._instance
        saved_initialized = tsw.TreeSitterWrapper._initialized
        tsw.TreeSitterWrapper._instance = None
        tsw.TreeSitterWrapper._initialized = False
        try:
            yield
        finally:
            tsw.TreeSitterWrapper._instance = saved_instance
            tsw.TreeSitterWrapper._initialized = saved_initialized

    def test_a_broken_grammar_leaves_the_run_alive_with_no_dart_parser(self, monkeypatch):
        """The ``except Exception`` arm of the Dart initialisation block: the wrapper
        still constructs and still serves every other language, with ``dart`` simply
        absent from ``parsers``.
        """
        from src.core import tree_sitter_wrapper as tsw

        class _BrokenGrammar:
            def language(self):
                raise RuntimeError("simulated broken Dart grammar")

        monkeypatch.setattr(tsw, "tree_sitter_dart", _BrokenGrammar(), raising=False)
        monkeypatch.setattr(tsw, "DART_AVAILABLE", True, raising=False)

        wrapper = tsw.TreeSitterWrapper()

        # The run survives, which is the whole point of the branch.
        assert wrapper.parsers, "a broken Dart grammar must not empty the parser registry"
        assert "dart" not in wrapper.parsers
        # Configuration still lists Dart, so .dart files are reported as skipped
        # rather than silently ignored.
        assert "dart" in wrapper.supported_languages

    def test_an_absent_grammar_leaves_the_run_alive_with_no_dart_parser(self, monkeypatch):
        """The import-failure arm sets ``DART_AVAILABLE`` false, so driving the flag
        drives the same outcome without having to unimport a loaded module.
        """
        from src.core import tree_sitter_wrapper as tsw

        monkeypatch.setattr(tsw, "DART_AVAILABLE", False, raising=False)

        wrapper = tsw.TreeSitterWrapper()

        assert wrapper.parsers, "an absent Dart grammar must not empty the parser registry"
        assert "dart" not in wrapper.parsers
        assert "dart" in wrapper.supported_languages
