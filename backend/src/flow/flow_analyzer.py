"""
Flow Analysis Engine for the Code Knowledge Base.

This module provides call graph construction, execution flow tracing,
and dependency analysis functionality.
"""

from typing import List, Dict, Optional, Set, Tuple, Any
from collections import defaultdict, deque
from pathlib import Path

from ..core.interfaces import IFlowAnalyzer, IComponentRepository
from ..core.models import (
    Component,
    ComponentType,
    ExecutionFlow,
    FlowStep,
    DependencyAnalysis,
    CallType,
    CouplingStrength,
)
from ..core.logging_utils import get_logger, ErrorContext
from ..core.tree_sitter_wrapper import TreeSitterWrapper, tree_cache_key
from .java_flow_support import (
    extract_java_parameter_types,
    extract_java_assignment_type,
    get_java_parameter_names,
    extract_java_field_types,
)
from .go_flow_support import (
    extract_go_parameter_types,
    extract_go_assignment_type,
    get_go_parameter_names,
)
from .kotlin_flow_support import (
    extract_kotlin_parameter_types,
    extract_kotlin_assignment_type,
    get_kotlin_parameter_names,
)
from .csharp_flow_support import (
    extract_csharp_parameter_types,
    extract_csharp_assignment_type,
    get_csharp_parameter_names,
)
from .rust_flow_support import (
    extract_rust_parameter_types,
    extract_rust_assignment_type,
    get_rust_parameter_names,
)
from .php_flow_support import (
    extract_php_parameter_types,
    extract_php_assignment_type,
    get_php_parameter_names,
)
from .cpp_flow_support import (
    extract_cpp_parameter_types,
    extract_cpp_assignment_type,
    get_cpp_parameter_names,
)
from .ruby_flow_support import (
    extract_ruby_assignment_type,
    get_ruby_parameter_names,
    extract_ruby_instance_attribute_types,
)
from .dart_flow_support import (
    extract_dart_parameter_types,
    extract_dart_assignment_type,
    get_dart_parameter_names,
    extract_dart_field_types,
)
from .typescript_flow_support import extract_typescript_field_types


logger = get_logger(__name__)

# Languages where an unqualified call inside a class can name another member of it.
_BARE_CALLS_REACH_MEMBERS = frozenset({"java", "csharp", "cpp", "kotlin", "dart", "c"})
# Component types whose body is a scope: a component nested in one is local to it.
_LOCAL_SCOPE_TYPES = (ComponentType.FUNCTION, ComponentType.METHOD, ComponentType.CONSTRUCTOR)
# Languages where a function defined inside a method is still visible outside it
# (a Ruby `def` inside a method defines a method of the enclosing class).
_NESTED_DEFINITIONS_ARE_GLOBAL = frozenset({"ruby"})


class FlowAnalyzer(IFlowAnalyzer):
    """
    Flow analysis engine providing call graph and dependency analysis.

    Analyzes component relationships, traces execution flows,
    and provides dependency analysis for the codebase.
    """

    def __init__(self, component_repository: IComponentRepository):
        """
        Initialize flow analyzer.

        Args:
            component_repository: Repository for accessing components
        """
        self._component_repository = component_repository
        self._call_graph: Dict[str, List[Tuple[str, int]]] = {}
        self._reverse_call_graph: Dict[str, List[str]] = {}
        self._call_graph_built = False
        self._tree_sitter_wrapper = TreeSitterWrapper()
        self._file_cache: Dict[str, Any] = {}  # Cache parsed AST trees
        self._inferred_param_types: Dict[
            str, Dict[int, str]
        ] = {}  # Inter-procedural type inference
        # Dart only: a named argument's type against the parameter it names.
        # Separate from the positional map above because a named argument's
        # call-site position says nothing about which parameter it fills.
        self._dart_inferred_named_param_types: Dict[str, Dict[str, str]] = {}
        self._typed_method_lookup: Dict[str, str] = {}
        # Same-file call resolution, filled by _build_component_lookup.
        self._file_and_parent_by_id: Dict[str, Tuple[Path, Optional[str], str]] = {}
        self._ids_by_file_and_name: Dict[Tuple[Path, str], List[Tuple[str, Optional[str]]]] = (
            defaultdict(list)
        )
        self._depth_cache: Optional[Dict[str, Any]] = None  # Cache for call graph depths
        self._scope_type_by_id: Dict[str, ComponentType] = {}
        self._span_by_id: Dict[str, Tuple[Path, int, int]] = {}  # file, first and last line

    def build_call_graph(
        self, components: List[Component], tree_cache: Optional[Dict[str, Any]] = None
    ) -> Dict[str, List[str]]:
        """
        Build call graph from components.

        Args:
            components: List of components to analyze
            tree_cache: Optional pre-parsed ASTs keyed by ``tree_cache_key``,
                handed over from CodeAnalyzer's phase-1 parse so this phase does
                not re-read and re-parse the same files. Keyed to match
                ``_process_file_components`` / ``_get_cached_syntax_tree``.

        Returns:
            Dictionary mapping component names to their called components (for backward compatibility)
        """
        with ErrorContext(logger, "build call graph"):
            # A fixed order gives the same lookup tables, and so the same graph,
            # for a full index and for a refresh (which appends changed files).
            components = sorted(
                components, key=lambda comp: (str(comp.file_path), comp.line_number, comp.id)
            )
            # Filter components to only include functions and methods
            callable_components = [
                comp
                for comp in components
                if comp.type in [ComponentType.FUNCTION, ComponentType.METHOD]
            ]
            logger.info(
                f"Building call graph for {len(callable_components)} callable components (filtered from {len(components)} total)"
            )

            self._call_graph.clear()
            self._reverse_call_graph.clear()
            self._dart_inferred_named_param_types.clear()
            self._file_cache.clear()
            self._inferred_param_types.clear()

            # Reuse phase-1 parsed trees when the caller hands them over, so the
            # call-extraction pass does not re-read and re-parse every file.
            # Cache misses still parse on demand, so a partial or absent cache is
            # always safe.
            if tree_cache:
                self._file_cache.update(tree_cache)

            # Initialize call graph with callable components only
            for component in callable_components:
                self._call_graph[component.id] = []

            # Build shared component lookup once (using all components for resolution)
            component_lookup = self._build_component_lookup(components)

            # Group callable components by file for batch processing
            components_by_file = defaultdict(list)
            for component in callable_components:
                components_by_file[str(component.file_path)].append(component)

            # First pass: Process callable components file by file
            for file_path, file_components in components_by_file.items():
                self._process_file_components(file_components, component_lookup, components)

            # Second pass: Re-process functions with inferred parameter types
            if self._inferred_param_types or self._dart_inferred_named_param_types:
                logger.debug(
                    f"Re-processing {len(self._inferred_param_types)} functions with inferred parameter types"
                )
                for file_path, file_components in components_by_file.items():
                    self._reprocess_with_inferred_types(
                        file_components, component_lookup, components
                    )

            # Build reverse call graph
            for comp_id, calls_with_lines in self._call_graph.items():
                for called_component, _ in calls_with_lines:
                    if called_component not in self._reverse_call_graph:
                        self._reverse_call_graph[called_component] = []
                    self._reverse_call_graph[called_component].append(comp_id)

            self._call_graph_built = True
            self._depth_cache = None  # Invalidate depth cache when call graph changes

            # Release the parsed trees now that call extraction is done — their
            # lifetime is scoped to this build. On-demand readers
            # (_get_cached_syntax_tree) re-parse on a miss, so clearing is safe.
            self._file_cache.clear()

            total_calls = sum(len(calls) for calls in self._call_graph.values())
            logger.info(f"Call graph built: {total_calls} total calls between components")

            # Return backward-compatible format (without line numbers)
            return {
                comp_id: [call[0] for call in calls] for comp_id, calls in self._call_graph.items()
            }

    def trace_execution_flow(self, start_component: str, depth: int = 10) -> ExecutionFlow:
        """
        Trace execution flow from starting component.

        Args:
            start_component: Name of component to start tracing from
            depth: Maximum depth to trace

        Returns:
            ExecutionFlow object containing traced flow

        Raises:
            ValueError: If start_component is not found
        """
        with ErrorContext(logger, f"trace execution flow from {start_component}"):
            if not self._call_graph_built:
                components = self._component_repository.get_all_components()
                self.build_call_graph(components)

            if start_component not in self._call_graph:
                raise ValueError(f"Component not found: {start_component}")

            flow = ExecutionFlow(start_component=start_component)
            visited = set()
            call_stack = []

            self._trace_recursive(start_component, flow, visited, call_stack, depth, 0)

            logger.debug(
                f"Traced execution flow: {len(flow.flow_steps)} steps, depth {flow.total_depth}"
            )
            return flow

    def analyze_dependencies(self, component: str) -> DependencyAnalysis:
        """
        Analyze dependencies for a component.

        Args:
            component: Name of component to analyze

        Returns:
            DependencyAnalysis with dependency information

        Raises:
            ValueError: If component is not found
        """
        with ErrorContext(logger, f"analyze dependencies for {component}"):
            if not self._call_graph_built:
                components = self._component_repository.get_all_components()
                self.build_call_graph(components)

            if component not in self._call_graph:
                # Component exists in catalog but not in the call graph
                # (e.g. classes, variables — only functions/methods have call graph entries).
                # Return an empty analysis instead of raising.
                return DependencyAnalysis(component=component)

            # Get direct dependencies. A component may call the same callee
            # multiple times at different lines; dependency analysis should
            # report unique relationships rather than every call site.
            direct_deps = self._unique_preserve_order(
                call[0] for call in self._call_graph.get(component, [])
            )

            # Calculate transitive dependencies
            transitive_deps = self._get_transitive_dependencies(component)

            # Get dependents (reverse dependencies)
            dependents = self._unique_preserve_order(self._reverse_call_graph.get(component, []))

            # Detect circular dependencies
            circular_deps = self._find_circular_dependencies_for_component(component)

            # Calculate dependency depth
            dep_depth = self._calculate_dependency_depth(component)

            # Assess coupling strength
            coupling = self._assess_coupling_strength(component, direct_deps, dependents)

            analysis = DependencyAnalysis(
                component=component,
                direct_dependencies=direct_deps,
                transitive_dependencies=transitive_deps,
                dependency_depth=dep_depth,
                circular_dependencies=circular_deps,
                coupling_strength=coupling,
                dependents=dependents,
            )

            logger.debug(
                f"Dependency analysis for {component}: {len(direct_deps)} direct, {len(transitive_deps)} transitive"
            )
            return analysis

    @staticmethod
    def _unique_preserve_order(values) -> List[str]:
        """Return unique strings in first-seen order."""
        seen = set()
        unique = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique

    def find_dependency_path(self, from_component: str, to_component: str) -> Optional[List[str]]:
        """
        Find dependency path between components.

        Args:
            from_component: Starting component
            to_component: Target component

        Returns:
            List of components in dependency path, or None if no path
        """
        if not self._call_graph_built:
            components = self._component_repository.get_all_components()
            self.build_call_graph(components)

        if from_component not in self._call_graph or to_component not in self._call_graph:
            return None

        # Use BFS to find shortest path
        queue = deque([(from_component, [from_component])])
        visited = {from_component}

        while queue:
            current, path = queue.popleft()

            if current == to_component:
                return path

            for neighbor, _ in self._call_graph.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

        return None

    def detect_circular_dependencies(self) -> List[List[str]]:
        """
        Detect circular dependencies in call graph.

        Returns:
            List of circular dependency chains
        """
        if not self._call_graph_built:
            components = self._component_repository.get_all_components()
            self.build_call_graph(components)

        cycles = []
        visited = set()
        rec_stack = set()

        def dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor, _ in self._call_graph.get(node, []):
                if neighbor in rec_stack:
                    # Found cycle
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    if cycle not in cycles:
                        cycles.append(cycle)
                elif neighbor not in visited:
                    dfs(neighbor, path.copy())

            rec_stack.remove(node)

        for component in self._call_graph:
            if component not in visited:
                dfs(component, [])

        logger.debug(f"Detected {len(cycles)} circular dependencies")
        return cycles

    def _build_component_lookup(self, components: List[Component]) -> Dict[str, str]:
        """Build call-name indexes for all components in linear passes.

        Call sites use several aliases (simple names, ``self.method``, static
        class calls, and instance-variable calls).  Build the supporting
        indexes once instead of scanning every component for every method.
        """
        component_lookup: Dict[str, str] = {}
        methods_by_class_suffix: Dict[str, List[Component]] = defaultdict(list)
        self._typed_method_lookup = {}
        self._file_and_parent_by_id = {}
        self._ids_by_file_and_name = defaultdict(list)
        self._scope_type_by_id = {}
        self._span_by_id = {}

        # Create the base lookup and collect the secondary indexes in one pass.
        for comp in components:
            simple_name = comp.get_simple_name()
            component_lookup[comp.id] = comp.id
            component_lookup[f"{comp.type.value}:{simple_name}"] = comp.id
            self._file_and_parent_by_id[comp.id] = (
                comp.file_path,
                comp.parent_component,
                comp.language,
            )
            self._scope_type_by_id[comp.id] = comp.type
            self._span_by_id[comp.id] = (comp.file_path, comp.line_number, comp.end_line_number)
            if comp.type in (ComponentType.FUNCTION, ComponentType.METHOD):
                self._ids_by_file_and_name[(comp.file_path, simple_name)].append(
                    (comp.id, comp.parent_component)
                )

            if (
                comp.type == ComponentType.CONSTRUCTOR
                and comp.language == "dart"
                and comp.parent_component
            ):
                # Dart alone calls a constructor by a name other than its class's:
                # `Store.named(1)`, and `Store()` whose member name is `new`.
                # Without both keys the resolver matches nothing and the edge is lost.
                class_name = comp.parent_component.split(".")[-1]
                component_lookup[f"{class_name}.{simple_name}"] = comp.id
                if simple_name == "new":
                    component_lookup[class_name] = comp.id

            if comp.type == ComponentType.METHOD and comp.parent_component:
                class_name = comp.parent_component.split(".")[-1]

                # NB: no `{class_name.lower()}.{method}` key. Lowercasing the
                # class name is another coincidental receiver-name guess — it
                # binds a receiver that merely spells the class name in lower
                # case (a `CheckoutItem` lambda parameter named `item`) to that
                # class (`Item.getTotalCost`), a false edge. An all-lowercase
                # receiver that is genuinely an instance is resolved by its
                # tracked type or a scoped field key, not by this. `self.m` and
                # the exact-case `Class.m` (explicit class-qualified call) stay.
                component_lookup[f"self.{simple_name}"] = comp.id
                component_lookup[f"{class_name}.{simple_name}"] = comp.id

                # _extract_instance_assignments previously matched methods with
                # parent_component.endswith(f".{class_name}").  Index every
                # suffix that begins after a dot to preserve that behavior for
                # both simple (Class) and qualified (module.Class) call names.
                parent = comp.parent_component
                for dot_index, char in enumerate(parent):
                    if char == "." and dot_index + 1 < len(parent):
                        class_suffix = parent[dot_index + 1 :]
                        methods_by_class_suffix[class_suffix].append(comp)
                        self._typed_method_lookup.setdefault(
                            f"{class_suffix}.{simple_name}", comp.id
                        )

        # Note: a `receiver.method` call whose receiver TYPE is unknown is left
        # unresolved on purpose. We used to alias `<var>.<method>` to a class
        # whose name merely matched the variable's name case-insensitively (a
        # global, unscoped guess), which bound e.g. a `CheckoutItem` lambda
        # parameter named `item` to an unrelated `Item` client model. A
        # coincidental same-name edge is worse than a missing one, so type-based
        # resolution (_resolve_call_with_types) and the scoped field/instance
        # mechanisms below are the only ways a qualified call resolves.
        self._add_instance_attribute_mappings(component_lookup, components, methods_by_class_suffix)
        self._build_scoped_lookup(component_lookup, components)

        return component_lookup

    def _process_file_components(
        self,
        file_components: List[Component],
        component_lookup: Dict[str, str],
        all_components: List[Component],
    ) -> None:
        """Process all components in a single file with shared AST."""
        if not file_components:
            return

        file_path = file_components[0].file_path
        language = file_components[0].language

        # Get or parse AST for this file
        cache_key = tree_cache_key(file_path, language)
        if cache_key not in self._file_cache:
            try:
                parse_result = self._tree_sitter_wrapper.parse_file(file_path, language)
                self._file_cache[cache_key] = parse_result.syntax_tree
            except Exception as e:
                logger.warning(f"Failed to parse {file_path}: {e}")
                return

        syntax_tree = self._file_cache[cache_key]

        # Process each component in this file
        for component in file_components:
            calls_with_lines = self._extract_calls_from_ast(
                syntax_tree,
                component.line_number - 1,  # Convert to 0-based
                component.end_line_number - 1,
                component_lookup,
                component.id,
                language,
                all_components,
            )

            # Remove duplicates while preserving order
            seen = set()
            unique_calls = []
            for call, line in calls_with_lines:
                if (call, line) not in seen:
                    seen.add((call, line))
                    unique_calls.append((call, line))

            self._call_graph[component.id] = unique_calls

    def _reprocess_with_inferred_types(
        self,
        file_components: List[Component],
        component_lookup: Dict[str, str],
        all_components: List[Component],
    ) -> None:
        """Re-process components that have inferred parameter types."""
        if not file_components:
            return

        file_path = file_components[0].file_path
        language = file_components[0].language
        cache_key = f"{file_path}:{language}"
        syntax_tree = self._file_cache.get(cache_key)

        if not syntax_tree:
            return

        # Only re-process components with inferred types. The Dart named-argument
        # map is checked too: a function inferred only from a named argument has
        # no positional entry, and would otherwise never be re-processed.
        for component in file_components:
            if (
                component.id in self._inferred_param_types
                or component.id in self._dart_inferred_named_param_types
            ):
                logger.debug(f"Re-processing {component.id} with inferred types")
                calls_with_lines = self._extract_calls_from_ast(
                    syntax_tree,
                    component.line_number - 1,
                    component.end_line_number - 1,
                    component_lookup,
                    component.id,
                    language,
                    all_components,
                )

                # Remove duplicates
                seen = set()
                unique_calls = []
                for call, line in calls_with_lines:
                    if (call, line) not in seen:
                        seen.add((call, line))
                        unique_calls.append((call, line))

                self._call_graph[component.id] = unique_calls

    def _build_scoped_lookup(
        self, component_lookup: Dict[str, str], components: List[Component]
    ) -> None:
        """Build nested-function aliases without parent-by-child cross scans.

        A component nested in a function (a local variable, a closure) is visible
        only in that function and in the functions nested in it, so it gets only
        the key ``<parent id>::<name>``. Every other component also keeps the
        first-component-wins key under its bare simple name.
        """
        component_ids = {comp.id for comp in components}
        for nested in components:
            parent_id = nested.parent_component
            simple_name = nested.get_simple_name()
            if parent_id and parent_id in component_ids:
                component_lookup[f"{parent_id}::{simple_name}"] = nested.id

            is_local = self._is_local_scope(parent_id, nested.id) and not (
                nested.language in _NESTED_DEFINITIONS_ARE_GLOBAL
                and nested.type in (ComponentType.FUNCTION, ComponentType.METHOD)
            )
            if simple_name not in component_lookup and not is_local:
                component_lookup[simple_name] = nested.id

    def _is_local_scope(self, scope_id: Optional[str], component_id: str) -> bool:
        """True when ``scope_id`` is a function-like component whose lines hold the component.

        The line check guards against a parent id that another component took over
        (a parser can give a function the id of the class it is named after).
        """
        if not scope_id or self._scope_type_by_id.get(scope_id) not in _LOCAL_SCOPE_TYPES:
            return False

        scope = self._span_by_id.get(scope_id)
        inner = self._span_by_id.get(component_id)
        if scope is None or inner is None:
            return False

        same_file = scope[0] == inner[0]
        return same_file and scope[1] <= inner[1] and inner[2] <= scope[2]

    def _enclosing_function_scopes(self, caller_component: str) -> List[str]:
        """Ids of the functions that enclose a caller, nearest first.

        Stops at a class body and on a cycle in the parent links.
        """
        scopes: List[str] = []
        current = caller_component
        parent = self._file_and_parent_by_id.get(current, (None, None, None))[1]
        while parent and parent not in scopes and self._is_local_scope(parent, current):
            scopes.append(parent)
            current, parent = parent, self._file_and_parent_by_id[parent][1]
        return scopes

    def _resolve_call_with_scope(
        self, call_name: str, caller_component: str, component_lookup: Dict[str, str]
    ) -> Optional[str]:
        """Resolve call name considering scope hierarchy."""
        # First try scoped resolution
        scoped_key = f"{caller_component}::{call_name}"
        if scoped_key in component_lookup:
            return component_lookup[scoped_key]

        # A function nested in an enclosing function is visible here too.
        for scope_id in self._enclosing_function_scopes(caller_component):
            nested_key = f"{scope_id}::{call_name}"
            if nested_key in component_lookup:
                return component_lookup[nested_key]

        # Fall back to regular resolution
        target = component_lookup.get(call_name)
        caller = self._file_and_parent_by_id.get(caller_component)
        resolved = self._file_and_parent_by_id.get(target) if target else None
        if caller is None or resolved is None or resolved[0] == caller[0]:
            return target
        # A same-named function in the caller's own file shadows the one elsewhere.
        return self._same_file_function(call_name, caller_component, caller) or target

    def _same_file_function(
        self, call_name: str, caller_component: str, caller: Tuple[Path, Optional[str], str]
    ) -> Optional[str]:
        """The function or method named call_name that a bare call in the caller's file reaches."""
        caller_file, caller_parent, language = caller
        # Only these languages let a bare call inside a class reach its own members.
        scopes = (caller_parent, None) if language in _BARE_CALLS_REACH_MEMBERS else (None,)
        candidates = self._ids_by_file_and_name.get((caller_file, call_name), ())
        for scope in scopes:
            for cid, parent in candidates:
                if parent == scope and cid != caller_component:
                    return cid
        return None

    def _add_instance_attribute_mappings(
        self,
        component_lookup: Dict[str, str],
        all_components: List[Component],
        methods_by_class_suffix: Dict[str, List[Component]],
    ) -> None:
        """Add mappings for instance attribute method calls like self._attr.method.

        Ruby is handled once per file rather than once per constructor: its
        instance variables are frequently established outside ``initialize`` (a
        memoized ``@client ||= Client.new`` in any method), so keying off the
        constructor alone would miss them.

        Dart likewise once per file, for a different reason: its fields are typed
        at class level with no constructor involvement in the commonest form.
        """
        ruby_files_seen: Set[Tuple[Any, str]] = set()
        dart_files_seen: Set[Tuple[Any, str]] = set()
        ts_files_seen: Set[Tuple[Any, str]] = set()
        java_files_seen: Set[Tuple[Any, str]] = set()

        for comp in all_components:
            if comp.language == "ruby":
                file_key = (comp.file_path, comp.language)
                if file_key in ruby_files_seen:
                    continue
                ruby_files_seen.add(file_key)
                try:
                    syntax_tree = self._get_cached_syntax_tree(comp)
                    self._extract_ruby_instance_assignments(
                        syntax_tree, component_lookup, methods_by_class_suffix
                    )
                except Exception as e:
                    logger.debug(f"Failed to map Ruby instance attributes in {comp.file_path}: {e}")
                continue

            if comp.language == "dart":
                file_key = (comp.file_path, comp.language)
                if file_key in dart_files_seen:
                    continue
                dart_files_seen.add(file_key)
                try:
                    syntax_tree = self._get_cached_syntax_tree(comp)
                    self._extract_dart_field_assignments(
                        syntax_tree, comp, component_lookup, methods_by_class_suffix
                    )
                except Exception as e:
                    logger.debug(f"Failed to map Dart field types in {comp.file_path}: {e}")
                continue

            if comp.language in ("typescript", "javascript"):
                # Once per file: a TS field is typed at class level (a field
                # definition or a constructor parameter property), with no
                # per-method involvement — the same shape as Dart.
                file_key = (comp.file_path, comp.language)
                if file_key in ts_files_seen:
                    continue
                ts_files_seen.add(file_key)
                try:
                    syntax_tree = self._get_cached_syntax_tree(comp)
                    self._extract_typescript_field_assignments(
                        syntax_tree, comp, component_lookup, methods_by_class_suffix
                    )
                except Exception as e:
                    logger.debug(
                        f"Failed to map TypeScript field types in {comp.file_path}: {e}"
                    )
                continue

            if comp.language == "java":
                # Once per file: a Java field is typed at class level (a field
                # declaration), with no per-method involvement — the same shape
                # as Dart.
                file_key = (comp.file_path, comp.language)
                if file_key in java_files_seen:
                    continue
                java_files_seen.add(file_key)
                try:
                    syntax_tree = self._get_cached_syntax_tree(comp)
                    self._extract_java_field_assignments(
                        syntax_tree, comp, component_lookup, methods_by_class_suffix
                    )
                except Exception as e:
                    logger.debug(f"Failed to map Java field types in {comp.file_path}: {e}")
                continue

            if comp.type == ComponentType.METHOD and comp.get_simple_name() == "__init__":
                try:
                    # Reuse the shared per-file AST cache instead of re-parsing the
                    # file once per __init__ method. This also pre-populates the
                    # cache so the later call-extraction pass reuses the same tree.
                    # The AST is identical to a fresh parse, so the instance-attribute
                    # mappings (and thus the call graph) are unchanged.
                    syntax_tree = self._get_cached_syntax_tree(comp)
                    self._extract_instance_assignments(
                        syntax_tree,
                        comp,
                        component_lookup,
                        methods_by_class_suffix,
                    )
                except Exception as e:
                    logger.debug(f"Failed to parse __init__ method {comp.id}: {e}")

    def _get_cached_syntax_tree(self, component: Component) -> Any:
        """Return the parsed tree for a component's file, populating the cache.

        Shares the per-file AST cache with the later call-extraction pass so a
        file is parsed at most once per run.
        """
        cache_key = tree_cache_key(component.file_path, component.language)
        syntax_tree = self._file_cache.get(cache_key)
        if syntax_tree is None:
            parse_result = self._tree_sitter_wrapper.parse_file(
                component.file_path, component.language
            )
            syntax_tree = parse_result.syntax_tree
            self._file_cache[cache_key] = syntax_tree
        return syntax_tree

    def _extract_ruby_instance_assignments(
        self,
        root_node: Any,
        component_lookup: Dict[str, str],
        methods_by_class_suffix: Dict[str, List[Component]],
    ) -> None:
        """Register ``@ivar.method`` lookup keys from ``@ivar = Foo.new``.

        The Ruby analogue of :meth:`_extract_instance_assignments`, which does
        the same for Python's ``self.attr = ClassName()``.
        """
        for ivar, class_name in extract_ruby_instance_attribute_types(
            root_node, self._get_node_text_simple
        ):
            for target_comp in methods_by_class_suffix.get(class_name, ()):
                component_lookup[f"{ivar}.{target_comp.get_simple_name()}"] = target_comp.id

    def _extract_dart_field_assignments(
        self,
        root_node: Any,
        file_component: Component,
        component_lookup: Dict[str, str],
        methods_by_class_suffix: Dict[str, List[Component]],
    ) -> None:
        """Register scoped ``<caller>::<field>.<method>`` keys from Dart field types.

        Scoped never bare: a bare key is CAPTURED by other languages, which register
        none. Target prefers the declaring file, then a unique Dart file, else nothing.
        """
        for declaring_class, field, class_name in extract_dart_field_types(
            root_node, self._get_node_text_simple
        ):
            targets = [
                comp
                for comp in methods_by_class_suffix.get(class_name, ())
                # A setter is excluded, not merely deduplicated: it shares its
                # getter's bare member name, so both would build the identical
                # key and the later write would decide the target by build
                # order. Only the getter is reachable by a call — a setter runs
                # on assignment, which is not one — so it is the right answer.
                if comp.language == "dart" and comp.attributes.accessor_kind != "set"
            ]
            local_targets = [comp for comp in targets if comp.file_path == file_component.file_path]
            if local_targets:
                targets = local_targets
            elif len({comp.file_path for comp in targets}) > 1:
                # Ambiguous across Dart files with no local declaration to
                # prefer: register nothing rather than let build order pick.
                continue
            if not targets:
                continue
            for caller in methods_by_class_suffix.get(declaring_class, ()):
                if caller.language != "dart" or caller.file_path != file_component.file_path:
                    continue
                for target_comp in targets:
                    key = f"{caller.id}::{field}.{target_comp.get_simple_name()}"
                    component_lookup[key] = target_comp.id

    def _extract_typescript_field_assignments(
        self,
        root_node: Any,
        file_component: Component,
        component_lookup: Dict[str, str],
        methods_by_class_suffix: Dict[str, List[Component]],
    ) -> None:
        """Register scoped ``<caller>::this.<field>.<method>`` keys from TS fields.

        The TS call-name for ``this.svc.update()`` is literally
        ``this.svc.update`` (see TypeScriptParser.extract_call_name), so the key
        carries the ``this.`` prefix verbatim. Scoped to the declaring class's
        own methods — never a bare key — so ``this.svc`` binds to the field's
        type and not to a same-named method on another class, and so other
        languages' resolution is unaffected.
        """
        for declaring_class, field, type_name in extract_typescript_field_types(
            root_node, self._get_node_text_simple
        ):
            targets = [
                comp
                for comp in methods_by_class_suffix.get(type_name, ())
                if comp.language in ("typescript", "javascript")
            ]
            local_targets = [
                comp for comp in targets if comp.file_path == file_component.file_path
            ]
            if local_targets:
                targets = local_targets
            elif len({comp.file_path for comp in targets}) > 1:
                # Ambiguous across files with no local declaration to prefer:
                # register nothing rather than let build order pick a winner.
                continue
            if not targets:
                continue
            for caller in methods_by_class_suffix.get(declaring_class, ()):
                if (
                    caller.language not in ("typescript", "javascript")
                    or caller.file_path != file_component.file_path
                ):
                    continue
                for target_comp in targets:
                    key = f"{caller.id}::this.{field}.{target_comp.get_simple_name()}"
                    component_lookup[key] = target_comp.id

    def _extract_java_field_assignments(
        self,
        root_node: Any,
        file_component: Component,
        component_lookup: Dict[str, str],
        methods_by_class_suffix: Dict[str, List[Component]],
    ) -> None:
        """Register scoped ``<caller>::<field>.<method>`` keys from Java fields.

        Java calls a field receiver two ways: bare (``cartsService.getCart()``)
        and through ``this`` (``this.cartsService.getCart()``).
        JavaParser.extract_call_name yields ``cartsService.getCart`` and
        ``this.cartsService.getCart`` respectively, so both key shapes are
        registered. Scoped to the declaring class's own methods in this file —
        never a bare key — so ``cartsService`` binds to the field's declared
        type and not to a same-named method on another class, and so other
        languages' resolution is unaffected (mirrors Dart/TypeScript).
        """
        for declaring_class, field, type_name in extract_java_field_types(
            root_node, self._get_node_text_simple
        ):
            targets = [
                comp
                for comp in methods_by_class_suffix.get(type_name, ())
                if comp.language == "java"
            ]
            local_targets = [
                comp for comp in targets if comp.file_path == file_component.file_path
            ]
            if local_targets:
                targets = local_targets
            elif len({comp.file_path for comp in targets}) > 1:
                # Ambiguous across files with no local declaration to prefer:
                # register nothing rather than let build order pick a winner.
                continue
            if not targets:
                continue
            for caller in methods_by_class_suffix.get(declaring_class, ()):
                if caller.language != "java" or caller.file_path != file_component.file_path:
                    continue
                for target_comp in targets:
                    method = target_comp.get_simple_name()
                    component_lookup[f"{caller.id}::{field}.{method}"] = target_comp.id
                    component_lookup[f"{caller.id}::this.{field}.{method}"] = target_comp.id

    def _extract_instance_assignments(
        self,
        root_node: Any,
        init_component: Component,
        component_lookup: Dict[str, str],
        methods_by_class_suffix: Dict[str, List[Component]],
    ) -> None:
        """Extract self._attr = ClassName() assignments from __init__ method."""

        def visit_node(node: Any) -> None:
            # Look for assignment statements: self._attr = ClassName()
            if node.type == "assignment":
                if len(node.children) >= 3:
                    left_node = node.children[0]  # self._attr
                    right_node = node.children[2]  # ClassName()

                    # Check if left side is self._attribute
                    if (
                        left_node.type == "attribute"
                        and len(left_node.children) >= 3
                        and self._get_node_text(left_node.children[0]) == "self"
                    ):
                        attr_name = self._get_node_text(left_node.children[2])

                        # Check if right side is a constructor call
                        if right_node.type == "call" and right_node.children:
                            class_name = self._get_node_text(right_node.children[0])

                            # Resolve methods through the precomputed class-suffix
                            # index instead of scanning every component per assignment.
                            for target_comp in methods_by_class_suffix.get(class_name, ()):
                                method_name = target_comp.get_simple_name()
                                attr_call = f"self.{attr_name}.{method_name}"
                                component_lookup[attr_call] = target_comp.id

            # Recursively visit children
            for child in node.children:
                visit_node(child)

        visit_node(root_node)

    def _get_node_text(self, node: Any) -> str:
        """Extract text content from AST node."""
        try:
            if hasattr(node, "text"):
                return node.text.decode("utf-8")
            return "unknown"
        except Exception:
            return "unknown"

    def _extract_calls_from_ast(
        self,
        root_node: Any,
        start_line: int,
        end_line: int,
        component_lookup: Dict[str, str],
        component_id: str,
        language: str,
        all_components: List[Component],
    ) -> List[Tuple[str, int]]:
        """Extract function calls with line numbers from AST nodes within line range."""
        calls_with_lines = []

        # Get language-specific parser
        language_parser = self._tree_sitter_wrapper.language_parsers.get(language)
        if not language_parser:
            logger.warning(f"No language parser available for {language}")
            return calls_with_lines

        # Extract local variable types for type-based resolution
        local_var_types = self._extract_local_variable_types(
            root_node, start_line, end_line, language, component_id
        )

        call_node_types = language_parser.get_call_node_types()
        function_node_types = language_parser.get_function_node_types()

        def visit_node(node: Any, inside_nested_function: bool = False) -> None:
            node_start_line = node.start_point[0]
            node_end_line = node.end_point[0]

            # Skip nodes completely outside our range
            if node_end_line < start_line or node_start_line > end_line:
                return

            in_range = start_line <= node_start_line <= end_line
            is_nested_function = (
                in_range and node.type in function_node_types and node_start_line > start_line
            )

            # Process call nodes
            if in_range and node.type in call_node_types and not inside_nested_function:
                self._process_call_node(
                    node,
                    calls_with_lines,
                    local_var_types,
                    component_lookup,
                    component_id,
                    language,
                    language_parser,
                    all_components,
                )

            # Process lambda expressions (Python)
            if (
                language == "python"
                and node.type == "lambda"
                and in_range
                and not inside_nested_function
            ):
                self._extract_calls_from_lambda(
                    node, calls_with_lines, component_lookup, component_id, language_parser
                )

            # Visit children
            if node_start_line <= end_line:
                for child in node.children:
                    visit_node(child, inside_nested_function or is_nested_function)

        visit_node(root_node)
        calls_with_lines.sort(key=lambda x: x[1])
        return calls_with_lines

    def _process_call_node(
        self,
        node: Any,
        calls_with_lines: List[Tuple[str, int]],
        local_var_types: Dict[str, str],
        component_lookup: Dict[str, str],
        component_id: str,
        language: str,
        language_parser: Any,
        all_components: List[Component],
    ) -> None:
        """Process a single call node and extract call information."""
        call_line = node.start_point[0] + 1
        # For new_expression, children[0] is "new" keyword, children[1] is the class name
        if node.type == "new_expression" and len(node.children) >= 2:
            function_node = node.children[1]
        else:
            # A leaf call node is the call name itself. Ruby needs this: a
            # paren-less zero-argument call (`notify`) is a bare `identifier`,
            # lexically identical to a local variable read, so the grammar emits
            # no call node to descend into. Every other language's call node
            # types are composite, so this falls through to children[0] as before.
            function_node = node.children[0] if node.children else node
        if not function_node:
            return

        call_name = language_parser.extract_call_name(function_node)
        if not call_name:
            return

        # Resolve call target
        target_id = self._resolve_call_with_types(call_name, local_var_types, component_lookup)
        if not target_id:
            target_id = self._resolve_call_with_scope(call_name, component_id, component_lookup)

        if target_id and target_id != component_id:
            calls_with_lines.append((target_id, call_line))

            # Store inferred types
            arg_types = self._extract_argument_types_at_call(node, local_var_types, language)
            if arg_types:
                self._store_inferred_param_types(target_id, arg_types, all_components)

            # Dart only: a named argument carries the parameter's own name, so
            # it is recorded by name instead of by position. A positional map
            # cannot express it — see _extract_dart_argument_types.
            if language == "dart":
                self._store_dart_named_param_types(node, target_id, local_var_types)

        # Handle React hooks
        if language in ("typescript", "javascript") and call_name in [
            "useCallback",
            "useMemo",
            "useEffect",
            "useLayoutEffect",
        ]:
            self._extract_calls_from_hook(
                node, calls_with_lines, component_lookup, component_id, language_parser
            )

    def _extract_calls_from_hook(
        self,
        hook_call_node: Any,
        calls_with_lines: List[Tuple[str, int]],
        component_lookup: Dict[str, str],
        component_id: str,
        language_parser: Any,
    ) -> None:
        """Extract calls from React hook functions like useCallback, useMemo, etc."""
        try:
            # Find the arguments node
            for child in hook_call_node.children:
                if child.type == "arguments":
                    # Look for arrow_function or function_expression in arguments
                    for arg in child.children:
                        if arg.type in ["arrow_function", "function_expression"]:
                            # Extract calls from the function body
                            self._extract_calls_from_function_body(
                                arg,
                                calls_with_lines,
                                component_lookup,
                                component_id,
                                language_parser,
                            )
                            break
                    break
        except Exception as e:
            logger.debug(f"Error extracting calls from hook: {e}")

    def _extract_calls_from_function_body(
        self,
        function_node: Any,
        calls_with_lines: List[Tuple[str, int]],
        component_lookup: Dict[str, str],
        component_id: str,
        language_parser: Any,
    ) -> None:
        """Extract calls from a function body (arrow function or function expression)."""
        call_node_types = language_parser.get_call_node_types()

        def visit_function_node(node: Any) -> None:
            # Look for call expressions in the function body
            if node.type in call_node_types:
                call_line = node.start_point[0] + 1  # Convert to 1-based line number
                # For new_expression, children[0] is "new", children[1] is the class
                if node.type == "new_expression" and len(node.children) >= 2:
                    function_node = node.children[1]
                else:
                    function_node = node.children[0] if node.children else None
                if function_node:
                    call_name = language_parser.extract_call_name(function_node)
                    logger.debug(f"Found call in hook function: {call_name} at line {call_line}")
                    if call_name:
                        target_id = self._resolve_call_with_scope(
                            call_name, component_id, component_lookup
                        )
                        if target_id and target_id != component_id:  # Don't include self-calls
                            calls_with_lines.append((target_id, call_line))
                            logger.debug(
                                f"Added hook call: {call_name} -> {target_id} at line {call_line}"
                            )

            # Recursively visit child nodes
            for child in node.children:
                visit_function_node(child)

        visit_function_node(function_node)

    def _extract_calls_from_lambda(
        self,
        lambda_node: Any,
        calls_with_lines: List[Tuple[str, int]],
        component_lookup: Dict[str, str],
        component_id: str,
        language_parser: Any,
    ) -> None:
        """Extract calls from Python lambda expressions."""
        try:
            # Find the lambda body (expression after ':')
            for child in lambda_node.children:
                if child.type not in ["lambda", "lambda_parameters", ":"]:
                    # This is the lambda body expression
                    self._extract_calls_from_function_body(
                        child, calls_with_lines, component_lookup, component_id, language_parser
                    )
                    break
        except Exception as e:
            logger.debug(f"Error extracting calls from lambda: {e}")

    def _get_node_text_simple(self, node: Any) -> str:
        """Get text content of a node."""
        try:
            # Use tree-sitter's built-in text extraction if available
            if hasattr(node, "text"):
                return node.text.decode("utf-8")
            # Fallback to basic approach
            return "unknown"
        except Exception:
            return "unknown"

    def _trace_recursive(
        self,
        component: str,
        flow: ExecutionFlow,
        visited: Set[str],
        call_stack: List[str],
        max_depth: int,
        current_depth: int,
    ) -> None:
        """Recursively trace execution flow."""
        if current_depth >= max_depth:
            flow.incomplete = True
            return

        if component in call_stack:
            # Circular call detected
            if component not in flow.circular_calls:
                flow.circular_calls.append(component)
            return

        call_stack.append(component)

        # Get calls sorted by line number
        calls_with_lines = self._call_graph.get(component, [])

        for called_component, line_number in calls_with_lines:
            step = FlowStep(
                sequence_number=len(flow.flow_steps) + 1,
                component=called_component,
                caller=component,
                call_type=CallType.DIRECT,
                file_location=f"{component}:{line_number}",
                line_number=line_number,
            )

            flow.add_step(step)

            if called_component not in visited:
                visited.add(called_component)
                self._trace_recursive(
                    called_component, flow, visited, call_stack.copy(), max_depth, current_depth + 1
                )

        call_stack.pop()

    def _get_transitive_dependencies(self, component: str) -> List[str]:
        """Get all transitive dependencies for a component."""
        transitive = set()
        visited = set()

        def dfs(node: str) -> None:
            if node in visited:
                return
            visited.add(node)

            for dep, _ in self._call_graph.get(node, []):
                if dep != component:  # Avoid self-reference
                    transitive.add(dep)
                    dfs(dep)

        for direct_dep, _ in self._call_graph.get(component, []):
            dfs(direct_dep)

        # Remove direct dependencies from transitive list
        direct_deps = set(call[0] for call in self._call_graph.get(component, []))
        return list(transitive - direct_deps)

    def _find_circular_dependencies_for_component(self, component: str) -> List[str]:
        """Find circular dependencies involving a specific component."""
        all_cycles = self.detect_circular_dependencies()
        component_cycles = []

        for cycle in all_cycles:
            if component in cycle:
                component_cycles.extend(cycle)

        return list(set(component_cycles))

    def _calculate_dependency_depth(self, component: str) -> int:
        """Calculate maximum dependency depth for a component."""
        visited = set()

        def dfs(node: str, depth: int) -> int:
            if node in visited:
                return depth
            visited.add(node)

            node_max_depth = depth
            for dep, _ in self._call_graph.get(node, []):
                dep_depth = dfs(dep, depth + 1)
                node_max_depth = max(node_max_depth, dep_depth)

            return node_max_depth

        return dfs(component, 0)

    def _assess_coupling_strength(
        self, component: str, dependencies: List[str], dependents: List[str]
    ) -> CouplingStrength:
        """Assess coupling strength based on dependency counts."""
        total_connections = len(dependencies) + len(dependents)

        if total_connections <= 2:
            return CouplingStrength.LOW
        elif total_connections <= 5:
            return CouplingStrength.MEDIUM
        else:
            return CouplingStrength.HIGH

    def _extract_local_variable_types(
        self, root_node: Any, start_line: int, end_line: int, language: str, component_id: str
    ) -> Dict[str, str]:
        """Extract variable->type mappings from assignments and parameters in function scope."""
        var_types = {}

        # First, add inferred parameter types from call sites (inter-procedural)
        if component_id in self._inferred_param_types:
            param_names = self._get_parameter_names(root_node, start_line, language)
            inferred_types = self._inferred_param_types[component_id]

            for param_idx, param_type in inferred_types.items():
                if param_idx < len(param_names):
                    param_name = param_names[param_idx]
                    var_types[param_name] = param_type
                    logger.debug(
                        f"Using inferred type for {component_id}.{param_name}: {param_type}"
                    )

        # Dart only: named arguments are recorded against the parameter they name,
        # so no sequence is consulted. Applied before the local annotations below,
        # so an annotation written on the parameter still wins.
        if language == "dart" and component_id in self._dart_inferred_named_param_types:
            var_types.update(self._dart_inferred_named_param_types[component_id])

        # Second, extract function parameters with type annotations (local)
        if language == "python":
            param_types = self._extract_python_parameter_types(root_node, start_line)
            var_types.update(param_types)
        elif language in ("typescript", "javascript"):
            param_types = self._extract_typescript_parameter_types(root_node, start_line)
            var_types.update(param_types)
        elif language == "java":
            param_types = extract_java_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)
        elif language == "go":
            param_types = extract_go_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)
        elif language == "kotlin":
            param_types = extract_kotlin_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)
        elif language == "csharp":
            param_types = extract_csharp_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)
        elif language == "rust":
            param_types = extract_rust_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)
        elif language == "php":
            param_types = extract_php_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)
        elif language in ("cpp", "c"):
            param_types = extract_cpp_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)
        elif language == "dart":
            param_types = extract_dart_parameter_types(
                root_node, start_line, self._get_node_text_simple
            )
            var_types.update(param_types)

        def visit_node(node: Any) -> None:
            # Check if node is within function scope
            node_start = node.start_point[0]
            node_end = node.end_point[0]

            # Skip nodes completely outside our range
            if node_end < start_line or node_start > end_line:
                return

            # Process nodes that overlap with our range
            in_range = start_line <= node_start <= end_line

            # Python: assignment node
            if in_range and language == "python" and node.type == "assignment":
                var_name, type_name = self._extract_python_assignment_type(node)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # TypeScript/JavaScript: variable_declarator
            elif (
                in_range
                and language in ("typescript", "javascript")
                and node.type == "variable_declarator"
            ):
                var_name, type_name = self._extract_typescript_assignment_type(node)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # Java: local_variable_declaration
            elif in_range and language == "java" and node.type == "local_variable_declaration":
                var_name, type_name = extract_java_assignment_type(node, self._get_node_text_simple)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # Go: var_declaration, short_var_declaration
            elif (
                in_range and language == "go" and node.type in ["var_spec", "short_var_declaration"]
            ):
                var_name, type_name = extract_go_assignment_type(node, self._get_node_text_simple)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # Kotlin: property_declaration
            elif in_range and language == "kotlin" and node.type == "property_declaration":
                var_name, type_name = extract_kotlin_assignment_type(
                    node, self._get_node_text_simple
                )
                if var_name and type_name:
                    var_types[var_name] = type_name

            # C#: local_declaration_statement
            elif in_range and language == "csharp" and node.type == "local_declaration_statement":
                var_name, type_name = extract_csharp_assignment_type(
                    node, self._get_node_text_simple
                )
                if var_name and type_name:
                    var_types[var_name] = type_name

            # Rust: let_declaration
            elif in_range and language == "rust" and node.type == "let_declaration":
                var_name, type_name = extract_rust_assignment_type(node, self._get_node_text_simple)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # PHP: assignment_expression ($x = new Foo())
            elif in_range and language == "php" and node.type == "assignment_expression":
                var_name, type_name = extract_php_assignment_type(node, self._get_node_text_simple)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # C/C++: declaration (Foo x; / Foo* p = new Foo();) or reassignment
            elif (
                in_range
                and language in ("cpp", "c")
                and node.type in ("declaration", "assignment_expression")
            ):
                var_name, type_name = extract_cpp_assignment_type(node, self._get_node_text_simple)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # Ruby: assignment / operator_assignment (x = Foo.new, @x ||= Foo.new)
            elif (
                in_range
                and language == "ruby"
                and node.type in ("assignment", "operator_assignment")
            ):
                var_name, type_name = extract_ruby_assignment_type(node, self._get_node_text_simple)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # Dart: `var r = Repo();`, each further variable of a multi-variable
            # declaration, a reassignment, and each typed `variable_pattern` —
            # which covers destructuring, if-case, switch case and pattern for-in.
            elif (
                in_range
                and language == "dart"
                and node.type
                in (
                    "local_variable_declaration",
                    "initialized_identifier",
                    "assignment_expression",
                    "variable_pattern",
                )
            ):
                var_name, type_name = extract_dart_assignment_type(node, self._get_node_text_simple)
                if var_name and type_name:
                    var_types[var_name] = type_name

            # Always recurse to children (they might be in range even if parent isn't)
            for child in node.children:
                visit_node(child)

        visit_node(root_node)
        return var_types

    def _extract_python_assignment_type(self, node: Any) -> Tuple[Optional[str], Optional[str]]:
        """Extract (var_name, type_name) from Python assignment."""
        if len(node.children) < 3:
            return None, None

        left_node = node.children[0]
        right_node = node.children[2]

        # Get variable name
        var_name = self._get_node_text_simple(left_node)

        # Pattern 1: api = EcommerceAPI()
        if right_node.type == "call" and right_node.children:
            constructor_node = right_node.children[0]
            # Handle simple identifier or attribute (e.g., module.Class)
            if constructor_node.type == "identifier":
                type_name = self._get_node_text_simple(constructor_node)
                return var_name, type_name
            elif constructor_node.type == "attribute":
                # For module.Class(), extract just the Class name
                if len(constructor_node.children) >= 3:
                    type_name = self._get_node_text_simple(constructor_node.children[2])
                    return var_name, type_name

        # Pattern 2: api: EcommerceAPI = ...
        if left_node.type == "typed_parameter":
            for child in left_node.children:
                if child.type == "type":
                    type_name = self._get_node_text_simple(child)
                    # Extract just variable name from typed_parameter
                    for subchild in left_node.children:
                        if subchild.type == "identifier":
                            var_name = self._get_node_text_simple(subchild)
                            return var_name, type_name

        return var_name, None

    def _extract_typescript_assignment_type(self, node: Any) -> Tuple[Optional[str], Optional[str]]:
        """Extract (var_name, type_name) from TypeScript variable declaration."""
        var_name = None
        type_name = None

        for child in node.children:
            # Get variable name
            if child.type == "identifier":
                var_name = self._get_node_text_simple(child)

            # Get type annotation
            elif child.type == "type_annotation":
                for subchild in child.children:
                    if subchild.type == "type_identifier":
                        type_name = self._get_node_text_simple(subchild)

            # Get type from constructor
            elif child.type == "new_expression":
                for subchild in child.children:
                    if subchild.type == "identifier":
                        type_name = self._get_node_text_simple(subchild)

        return var_name, type_name

    def _extract_python_parameter_types(
        self, node: Any, function_start_line: int
    ) -> Dict[str, str]:
        """Extract parameter type annotations from Python function definition."""
        param_types = {}

        # Only process if this is the function definition at the start line
        if node.type == "function_definition" and node.start_point[0] == function_start_line:
            # Found the function, extract parameters
            for child in node.children:
                if child.type == "parameters":
                    for param_child in child.children:
                        if param_child.type == "typed_parameter":
                            # Extract param_name: Type
                            param_name = None
                            type_name = None
                            for subchild in param_child.children:
                                if subchild.type == "identifier":
                                    param_name = self._get_node_text_simple(subchild)
                                elif subchild.type == "type":
                                    type_name = self._get_node_text_simple(subchild)
                            if param_name and type_name:
                                param_types[param_name] = type_name
                                logger.debug(
                                    f"Extracted parameter type: {param_name} -> {type_name}"
                                )
            return param_types

        # Search children recursively
        for child in node.children:
            param_types.update(self._extract_python_parameter_types(child, function_start_line))

        return param_types

    def _extract_typescript_parameter_types(
        self, node: Any, function_start_line: int
    ) -> Dict[str, str]:
        """Extract parameter type annotations from TypeScript function definition."""
        param_types = {}

        # Look for function/method declaration at start line
        if node.start_point[0] != function_start_line:
            for child in node.children:
                if child.start_point[0] <= function_start_line:
                    param_types.update(
                        self._extract_typescript_parameter_types(child, function_start_line)
                    )
            return param_types

        # Extract from formal_parameters
        for child in node.children:
            if child.type == "formal_parameters":
                for param_child in child.children:
                    if param_child.type == "required_parameter":
                        param_name = None
                        type_name = None
                        for subchild in param_child.children:
                            if subchild.type == "identifier":
                                param_name = self._get_node_text_simple(subchild)
                            elif subchild.type == "type_annotation":
                                for type_child in subchild.children:
                                    if type_child.type == "type_identifier":
                                        type_name = self._get_node_text_simple(type_child)
                        if param_name and type_name:
                            param_types[param_name] = type_name

        return param_types

    def _resolve_call_with_types(
        self, call_name: str, local_var_types: Dict[str, str], component_lookup: Dict[str, str]
    ) -> Optional[str]:
        """Resolve call using local variable type information."""
        # Check if this is an attribute call (obj.method)
        if "." not in call_name:
            return None

        parts = call_name.split(".", 1)
        var_name = parts[0]
        method_name = parts[1]

        # Look up variable type
        class_name = local_var_types.get(var_name)
        if not class_name:
            return None

        # Try to resolve ClassName.method_name
        resolved_call = f"{class_name}.{method_name}"
        target_id = component_lookup.get(resolved_call)

        if target_id:
            logger.debug(f"Resolved {call_name} -> {target_id} via type {class_name}")
            return target_id

        # Resolve qualified type names (for example, api.EcommerceAPI) through
        # the first-match suffix index built alongside component_lookup.
        target_id = self._typed_method_lookup.get(resolved_call)
        if target_id:
            logger.debug(f"Resolved {call_name} -> {target_id} via full path")
            return target_id

        return None

    def _extract_argument_types_at_call(
        self, call_node: Any, local_var_types: Dict[str, str], language: str
    ) -> List[Optional[str]]:
        """Extract types of arguments passed in a function call."""
        arg_types = []

        # Dart buries arguments below its call node, so the shared loop finds
        # none and the pass never fired for Dart. Gated on language because
        # eight verified call graphs share this reader; its own test proves it.
        if language == "dart":
            return self._extract_dart_argument_types(call_node, local_var_types)

        # Find arguments node
        for child in call_node.children:
            if child.type in ["argument_list", "arguments"]:
                for arg_child in child.children:
                    # Skip punctuation
                    if arg_child.type in ["(", ")", ","]:
                        continue

                    # Extract argument type
                    if arg_child.type == "identifier":
                        arg_name = self._get_node_text_simple(arg_child)
                        arg_type = local_var_types.get(arg_name)
                        arg_types.append(arg_type)
                    elif arg_child.type == "attribute":
                        # For obj.attr, try to resolve the type
                        arg_name = self._get_node_text_simple(arg_child)
                        arg_type = local_var_types.get(arg_name)
                        arg_types.append(arg_type)
                    else:
                        # Literal, expression, or complex value
                        arg_types.append(None)

        return arg_types

    def _extract_dart_argument_types(
        self, call_node: Any, local_var_types: Dict[str, str]
    ) -> List[Optional[str]]:
        """Type the POSITIONAL arguments of one Dart call, in positional order.

        Dart buries arguments under ``argument_part``, where the shared reader does not
        look. A named argument gets no positional entry; it is matched by its label.
        """
        arguments = self._dart_arguments_node(call_node)
        if arguments is None:
            return []

        arg_types: List[Optional[str]] = []
        for child in arguments.children:
            if child.type != "argument":
                continue
            arg_types.append(self._dart_argument_type(child, local_var_types))
        return arg_types

    @staticmethod
    def _dart_arguments_node(call_node: Any) -> Optional[Any]:
        """Find the ``arguments`` node of a Dart call, through ``argument_part``."""
        for child in call_node.children:
            if child.type == "arguments":
                return child
            if child.type == "argument_part":
                for inner in child.children:
                    if inner.type == "arguments":
                        return inner
        return None

    def _dart_argument_type(self, wrapper: Any, local_var_types: Dict[str, str]) -> Optional[str]:
        """Return the type of one Dart argument expression, or None.

        Only a BARE identifier is typed. Dart's grammar is flat, so `repo.clone()`
        wraps `identifier(repo)` plus selectors, and reading that identifier would
        report the receiver's type for an expression whose value is the call's
        result. That is a wrong binding rather than a missing one, so anything
        composite yields None — the same answer the shared reader gives.
        """
        children = [child for child in wrapper.children if child.type != "comment"]
        if len(children) != 1 or children[0].type != "identifier":
            return None
        return local_var_types.get(self._get_node_text_simple(children[0]))

    def _get_parameter_names(
        self, root_node: Any, function_start_line: int, language: str
    ) -> List[str]:
        """Extract parameter names from function definition in order."""
        param_names = []

        if language == "python":
            param_names = self._get_python_parameter_names(root_node, function_start_line)
        elif language in ("typescript", "javascript"):
            param_names = self._get_typescript_parameter_names(root_node, function_start_line)
        elif language == "java":
            param_names = get_java_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language == "go":
            param_names = get_go_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language == "kotlin":
            param_names = get_kotlin_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language == "csharp":
            param_names = get_csharp_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language == "rust":
            param_names = get_rust_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language == "php":
            param_names = get_php_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language in ("cpp", "c"):
            param_names = get_cpp_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language == "ruby":
            param_names = get_ruby_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )
        elif language == "dart":
            # Live since the Dart-gated descent landed: 16,146 parameter bindings
            # over three repositories, but exactly one new edge. Returns POSITIONAL
            # names only — declaration order misbinds a named argument (measured).
            param_names = get_dart_parameter_names(
                root_node, function_start_line, self._get_node_text_simple
            )

        return param_names

    def _get_python_parameter_names(self, node: Any, function_start_line: int) -> List[str]:
        """Extract Python parameter names in order."""
        param_names = []

        if node.type == "function_definition" and node.start_point[0] == function_start_line:
            for child in node.children:
                if child.type == "parameters":
                    for param_child in child.children:
                        if param_child.type == "identifier":
                            param_names.append(self._get_node_text_simple(param_child))
                        elif param_child.type == "typed_parameter":
                            for subchild in param_child.children:
                                if subchild.type == "identifier":
                                    param_names.append(self._get_node_text_simple(subchild))
                                    break
                        elif param_child.type == "default_parameter":
                            if param_child.children:
                                param_names.append(
                                    self._get_node_text_simple(param_child.children[0])
                                )
            return param_names

        # Search children
        for child in node.children:
            result = self._get_python_parameter_names(child, function_start_line)
            if result:
                return result

        return param_names

    def _get_typescript_parameter_names(self, node: Any, function_start_line: int) -> List[str]:
        """Extract TypeScript parameter names in order."""
        param_names = []

        if node.start_point[0] == function_start_line:
            for child in node.children:
                if child.type == "formal_parameters":
                    for param_child in child.children:
                        if param_child.type in ["required_parameter", "optional_parameter"]:
                            for subchild in param_child.children:
                                if subchild.type == "identifier":
                                    param_names.append(self._get_node_text_simple(subchild))
                                    break
            if param_names:
                return param_names

        # Search children
        for child in node.children:
            result = self._get_typescript_parameter_names(child, function_start_line)
            if result:
                return result

        return param_names

    def _store_dart_named_param_types(
        self, call_node: Any, target_function_id: str, local_var_types: Dict[str, str]
    ) -> None:
        """Record a Dart named argument's type against the parameter it names.

        A label identifies its parameter, so no positional alignment applies. Separate
        from the shared positional map, whose index cannot express a name. First wins.
        """
        arguments = self._dart_arguments_node(call_node)
        if arguments is None:
            return

        for child in arguments.children:
            if child.type != "named_argument":
                continue
            label = next((part for part in child.children if part.type == "label"), None)
            if label is None:
                continue
            name = next(
                (
                    self._get_node_text_simple(part)
                    for part in label.children
                    if part.type == "identifier"
                ),
                None,
            )
            if not name or name == "unknown":
                continue
            # The label's own identifier is nested inside the `label` node, so the
            # value is whatever else the wrapper holds. Only a BARE identifier is
            # typed, as on the positional path: `handler: repo.clone()` puts
            # `repo` here followed by selectors, and reading it would bind the
            # receiver's type to a parameter whose value is the call's result.
            value_parts = [
                part for part in child.children if part.type not in ("label", "comment")
            ]
            if len(value_parts) != 1 or value_parts[0].type != "identifier":
                continue
            arg_type = local_var_types.get(self._get_node_text_simple(value_parts[0]))
            if not arg_type:
                continue
            self._dart_inferred_named_param_types.setdefault(target_function_id, {}).setdefault(
                name, arg_type
            )

    def _store_inferred_param_types(
        self,
        target_function_id: str,
        arg_types: List[Optional[str]],
        all_components: List[Component],
    ) -> None:
        """Store inferred parameter types for a function based on call site arguments."""
        if not arg_types:
            return

        # Initialize if not exists
        if target_function_id not in self._inferred_param_types:
            self._inferred_param_types[target_function_id] = {}

        # Store each argument type by position
        for idx, arg_type in enumerate(arg_types):
            if arg_type:
                # Only store if we don't already have a type for this parameter
                # or if it's the same type (for consistency)
                existing_type = self._inferred_param_types[target_function_id].get(idx)
                if existing_type is None:
                    self._inferred_param_types[target_function_id][idx] = arg_type
                    logger.debug(f"Inferred type for {target_function_id} param {idx}: {arg_type}")
                elif existing_type != arg_type:
                    # Multiple different types - could use union types in future
                    logger.debug(
                        f"Conflicting types for {target_function_id} param {idx}: {existing_type} vs {arg_type}"
                    )

    def get_call_depths(self) -> Dict[str, int]:
        """Calculate call depth for all callable components.

        Call depth = maximum number of levels of calls below a component.
        Entry points (functions with deepest call trees) have highest depth.

        Returns:
            Dict mapping component_id to its call depth
        """
        if self._depth_cache is not None:
            return self._depth_cache["depths"]

        # Get callable components
        all_components = self._component_repository.get_all_components()
        callable_components = [
            c for c in all_components if c.type in [ComponentType.FUNCTION, ComponentType.METHOD]
        ]
        callable_ids = {c.id for c in callable_components}

        # Build filtered call graph (only callable-to-callable)
        filtered_cg = self._build_filtered_call_graph(callable_ids)

        # Calculate depths. Iterative + memoized, O(V+E) — see _calculate_all_depths.
        import time

        edge_count = sum(len(v) for v in filtered_cg.values())
        logger.info(
            f"Computing call depths for {len(callable_ids)} callable components "
            f"({edge_count} edges)..."
        )
        _t0 = time.perf_counter()
        depths = self._calculate_all_depths(filtered_cg, callable_ids)

        # Group by depth
        depth_groups = {}
        for comp_id, depth in depths.items():
            if depth not in depth_groups:
                depth_groups[depth] = []
            depth_groups[depth].append(comp_id)

        max_depth = max(depths.values()) if depths else 0
        logger.info(
            f"Call depths computed in {time.perf_counter() - _t0:.1f}s (max_depth={max_depth})"
        )

        # Cache results
        self._depth_cache = {
            "depths": depths,
            "depth_groups": depth_groups,
            "max_depth": max_depth,
            "filtered_cg": filtered_cg,
        }

        return depths

    def get_components_by_depth(self, depth: int) -> List[str]:
        """Get component IDs at specific call depth.

        Args:
            depth: Call depth level

        Returns:
            List of component IDs at that depth
        """
        self.get_call_depths()  # Ensure calculated
        return self._depth_cache["depth_groups"].get(depth, [])

    def get_max_depth(self) -> int:
        """Get maximum call depth in call graph.

        Returns:
            Maximum depth value
        """
        self.get_call_depths()  # Ensure calculated
        return self._depth_cache["max_depth"]

    def count_components_at_depth(self, depth: int) -> int:
        """Count components at specific call depth.

        Args:
            depth: Call depth level

        Returns:
            Number of components at that depth
        """
        return len(self.get_components_by_depth(depth))

    def _build_filtered_call_graph(self, callable_ids: Set[str]) -> Dict[str, List[str]]:
        """Build call graph filtered to only callable components.

        Args:
            callable_ids: Set of callable component IDs

        Returns:
            Filtered call graph
        """
        filtered_cg = {}
        for comp_id in callable_ids:
            if comp_id in self._call_graph:
                deps = self._call_graph[comp_id]
                callable_calls = []
                if isinstance(deps, list):
                    for dep in deps:
                        if isinstance(dep, (list, tuple)) and len(dep) >= 1:
                            func_name = dep[0]
                            if func_name in callable_ids:
                                callable_calls.append(func_name)
                        elif isinstance(dep, str) and dep in callable_ids:
                            callable_calls.append(dep)
                filtered_cg[comp_id] = callable_calls
            else:
                filtered_cg[comp_id] = []
        return filtered_cg

    def _calculate_all_depths(
        self, call_graph: Dict[str, List[str]], callable_ids: Set[str]
    ) -> Dict[str, int]:
        """Call depth for every callable component. Iterative, memoized, O(V+E).

        depth(node) = 0 if it has no callable callees, else
                      1 + max(depth(callee)) over its callees.

        Computes each node's depth exactly once with an explicit stack, memoizing results
        across all roots, and breaks cycles by treating a back-edge to a node still on the
        stack as a 0 contribution.
        """
        UNVISITED, ON_STACK, DONE = 0, 1, 2
        state: Dict[str, int] = {}
        depth: Dict[str, int] = {}

        for root in callable_ids:
            if state.get(root, UNVISITED) == DONE:
                continue
            # Frame: [node, children, next_child_index, max_child_depth_plus_one]
            stack: List[list] = [[root, list(call_graph.get(root, [])), 0, 0]]
            state[root] = ON_STACK
            while stack:
                frame = stack[-1]
                node, children, i = frame[0], frame[1], frame[2]
                if i < len(children):
                    child = children[i]
                    child_state = state.get(child, UNVISITED)
                    if child_state == DONE:
                        # Memoized: fold in the child's depth, then advance.
                        cand = depth[child] + 1
                        if cand > frame[3]:
                            frame[3] = cand
                        frame[2] += 1
                    elif child_state == ON_STACK:
                        # Cycle back-edge: contributes 0. Advance past it.
                        frame[2] += 1
                    else:
                        # Descend. Do NOT advance i — revisit this child (now
                        # DONE) when we return, so its depth is folded in.
                        state[child] = ON_STACK
                        stack.append([child, list(call_graph.get(child, [])), 0, 0])
                else:
                    # All children processed — finalize this node.
                    stack.pop()
                    depth[node] = frame[3]
                    state[node] = DONE

        # Any callable never reached above (shouldn't happen since every id is a
        # root) still gets a depth of 0.
        for cid in callable_ids:
            depth.setdefault(cid, 0)
        return depth

    def build_call_graph_with_source(
        self, component_id: str, max_depth: int, source_root: Optional[Path] = None
    ) -> Optional[Dict[str, Any]]:
        """Build call graph with source code for a component.

        Args:
            component_id: Starting component ID
            max_depth: Maximum depth to traverse
            source_root: Root that relative component paths resolve against

        Returns:
            Dictionary containing call graph with source code, or None if not found
        """
        return self._build_call_graph_with_source_recursive(
            component_id, max_depth, set(), source_root
        )

    def _build_call_graph_with_source_recursive(
        self,
        component_id: str,
        max_depth: int,
        visited: Set[str],
        source_root: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        """Recursively build call graph with source code."""
        if max_depth <= 0 or component_id in visited:
            return None

        visited.add(component_id)
        comp = self._component_repository.find_component(component_id)

        if not comp:
            return None

        # Read source code from file
        source = "Source not available"
        file_path = Path(comp.file_path)
        # Spaces refreshed by older releases stored repo-relative paths.
        if source_root is not None and not file_path.is_absolute():
            file_path = source_root / file_path
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                source = "".join(lines[comp.line_number - 1 : comp.end_line_number])
        except Exception as e:
            source = f"Error reading source: {e}"

        node = {
            "id": component_id,
            "type": comp.type.value,
            "file": str(comp.file_path),
            "lines": f"{comp.line_number}-{comp.end_line_number}",
            "source": source,
            "calls": [],
        }

        # Get direct calls from the call graph.
        # Share ONE visited set across the whole traversal (no per-child copy):
        # each component is expanded at most once, making this O(V+E) instead of
        # exponential on dense graphs (a function reachable by many paths was
        # previously re-expanded on every path). Behavior-preserving: the
        # consumers (_format_call_graph in flow_tools and topdown_summarizer)
        # already de-duplicate by id with a `seen` set, so the flattened
        # first-occurrence tree renders identically.
        calls_with_lines = self._call_graph.get(component_id, [])
        for called_comp, _ in calls_with_lines:
            child_node = self._build_call_graph_with_source_recursive(
                called_comp, max_depth - 1, visited, source_root
            )
            if child_node:
                node["calls"].append(child_node)

        return node
