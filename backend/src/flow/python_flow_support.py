"""Python name resolution for the call graph.

Python finds a method through the class of the receiver and its bases (the MRO).
It finds a bare name through the enclosing functions, the module, and the
module's imports. This module applies those rules to ``self.x()``, ``cls.x()``,
``super().x()`` and bare ``x()`` calls. Each rule gives one target or no target:
it never selects a same-named method of an unrelated class, or a local of a
different function. A missing edge is better than a coincidental one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from ..core.logging_utils import get_logger
from ..core.models import Component, ComponentType

logger = get_logger(__name__)

# Base classes that add no methods a call graph needs; they are skipped in the MRO.
_IGNORED_BASES = frozenset({"object", "Generic", "Protocol", "ABC"})
_RECEIVERS = frozenset({"self", "cls"})
_SUPER_PREFIX = "super()."
# Re-export chains (``from .app import Flask`` in ``__init__.py``) are short.
_MAX_IMPORT_DEPTH = 4
_EXTERNAL = "<external>"
_MODULE_SCOPE_TYPES = (
    ComponentType.FUNCTION,
    ComponentType.CLASS,
    ComponentType.VARIABLE,
    ComponentType.CONSTANT,
)
_SCOPE_TYPES = (ComponentType.FUNCTION, ComponentType.METHOD)
# A module that is not indexed (it only re-exports names) is still read for its
# imports, but not when it is larger than this: FileScanner's size limit does not
# apply to it, and a re-export module is small.
_MAX_UNINDEXED_MODULE_BYTES = 1 << 20

# (module spec, imported name); the name is None for ``import a.b [as c]``.
ImportBinding = Tuple[str, Optional[str]]


class _ModuleIndex:
    """What the resolver needs from one parsed file, collected in a single walk."""

    def __init__(self, root: Any) -> None:
        # (node type, 0-based line of the `class` or `def` keyword) -> node. The
        # Python parser gives a component that line too, also under a decorator.
        self.definitions: Dict[Tuple[str, int], Any] = {}
        self.imports: Dict[str, ImportBinding] = {}
        self.star_imports: List[str] = []

        for node in _walk(root) if root is not None else ():
            if node.type in ("class_definition", "function_definition"):
                self.definitions[(node.type, node.start_point[0])] = node

            elif node.type == "import_statement":
                for item in node.children_by_field_name("name"):
                    _bind_import(self.imports, item, None)

            elif node.type == "import_from_statement":
                module = node.child_by_field_name("module_name")
                spec = module.text.decode("utf-8") if module is not None else ""
                if any(child.type == "wildcard_import" for child in node.children):
                    self.star_imports.append(spec)
                for item in node.children_by_field_name("name"):
                    _bind_import(self.imports, item, spec)


class PythonNameResolver:
    """Resolve Python call names with the receiver's MRO and Python scope rules.

    Args:
        components: All components of the analysis. Only Python components are indexed.
        syntax_tree_for: Returns the parsed root node of a Python file. Modules that
            only re-export names (``__init__.py``) have no components, so the
            resolver reads them by path.
    """

    def __init__(
        self,
        components: List[Component],
        syntax_tree_for: Callable[[Path], Any],
    ) -> None:
        self._syntax_tree_for = syntax_tree_for

        # Components by id and by scope.
        self._by_id: Dict[str, Component] = {}
        self._any_component_in_file: Dict[Path, Component] = {}
        self._top_level: Dict[Path, Dict[str, str]] = {}  # file -> name -> id
        self._methods: Dict[str, Dict[str, str]] = {}  # class id -> name -> id
        self._children: Dict[str, Dict[str, str]] = {}  # parent id -> name -> id
        self._index_components(components)

        # The analyzed tree: imports must not lead out of it.
        self._files = sorted(self._any_component_in_file)
        self._root = (
            Path(os.path.commonpath([f.parent for f in self._files])) if self._files else None
        )
        self._package_parents = self._find_package_parents()

        # Filled on demand.
        self._module_indexes: Dict[Path, _ModuleIndex] = {}
        self._module_files: Dict[Tuple[Optional[Path], str], Optional[Path]] = {}
        self._folder_entries_cache: Dict[Path, Set[str]] = {}
        self._source_folders_cache: Optional[List[Path]] = None
        self._import_roots: Dict[str, List[Path]] = {}
        self._class_bases_cache: Dict[str, List[str]] = {}
        self._mro_cache: Dict[str, List[str]] = {}
        self._parameter_names_cache: Dict[str, Set[str]] = {}
        self._errors = 0

    def _index_components(self, components: List[Component]) -> None:
        for comp in components:
            if comp.language != "python":
                continue
            self._by_id[comp.id] = comp
            self._any_component_in_file.setdefault(comp.file_path, comp)

        # Python binds a name to its last definition (an ``@overload`` group ends
        # with the implementation), so sort by line and let later ones win.
        in_source_order = sorted(
            self._by_id.values(), key=lambda c: (str(c.file_path), c.line_number)
        )
        for comp in in_source_order:
            name = comp.get_simple_name()
            parent = self._parent(comp)
            if parent is not None:
                self._children.setdefault(parent.id, {})[name] = comp.id
                if parent.type == ComponentType.CLASS and comp.type == ComponentType.METHOD:
                    self._methods.setdefault(parent.id, {})[name] = comp.id
            elif not comp.parent_component and comp.type in _MODULE_SCOPE_TYPES:
                self._top_level.setdefault(comp.file_path, {})[name] = comp.id

    def _find_package_parents(self) -> Dict[str, Set[Path]]:
        """Folder name -> folders that hold a folder with that name.

        ``import a.b`` starts from a folder that holds ``a``. The analyzed root may
        itself be the package ``a``, so its own name counts too.
        """
        parents: Dict[str, Set[Path]] = {}
        if self._root is None:
            return parents

        first = max(len(self._root.parts) - 1, 0)
        for file in self._files:
            for index in range(first, len(file.parts) - 1):
                parents.setdefault(file.parts[index], set()).add(Path(*file.parts[:index]))
        return parents

    def handles(self, call_name: str) -> bool:
        """Return True for the call shapes this resolver owns."""
        if call_name.startswith(_SUPER_PREFIX):
            return "." not in call_name[len(_SUPER_PREFIX) :]

        head, _, rest = call_name.partition(".")
        return (head in _RECEIVERS and bool(rest) and "." not in rest) or "." not in call_name

    def resolve(self, call_name: str, caller_id: str) -> Tuple[bool, Optional[str]]:
        """Resolve one call of a Python caller.

        Returns:
            ``(False, None)`` when the caller is not Python or the call shape is not
            handled here; the caller then uses its other rules. ``(True, target)``
            otherwise, where ``target`` is None when Python's rules do not give one
            component in the repository.
        """
        caller = self._by_id.get(caller_id)
        if caller is None or not self.handles(call_name):
            return False, None

        try:
            if call_name.startswith(_SUPER_PREFIX):
                return True, self._resolve_receiver(caller, call_name[len(_SUPER_PREFIX) :], True)
            head, _, rest = call_name.partition(".")
            if rest:
                return True, self._resolve_receiver(caller, rest, False)
            return True, self._resolve_bare_name(caller, head)
        except Exception as e:  # one odd syntax tree must not fail the whole call graph
            self._errors += 1
            log = logger.warning if self._errors == 1 else logger.debug
            log(f"Python name resolution failed for {call_name} in {caller_id}: {e}")
            return True, None

    # --- receivers ---

    def _resolve_receiver(
        self, caller: Component, method_name: str, skip_own_class: bool
    ) -> Optional[str]:
        class_id = self._enclosing_class(caller)
        if class_id is None:
            return None

        mro = self._mro(class_id)
        for klass in mro[1:] if skip_own_class else mro:
            if klass.startswith(_EXTERNAL):
                return None
            target = self._methods.get(klass, {}).get(method_name)
            if target is not None:
                return target
        return None

    def _enclosing_class(self, caller: Component) -> Optional[str]:
        """Nearest enclosing class. A closure in a method uses that method's ``self``."""
        for ancestor in self._ancestors(caller):
            if ancestor.type == ComponentType.CLASS:
                return ancestor.id
        return None

    def _parent(self, comp: Component) -> Optional[Component]:
        return self._by_id.get(comp.parent_component) if comp.parent_component else None

    def _ancestors(self, comp: Component) -> Iterator[Component]:
        """Parents of a component, nearest first. Stops on a cycle in the parent links."""
        seen = {comp.id}
        parent = self._parent(comp)
        while parent is not None and parent.id not in seen:
            seen.add(parent.id)
            yield parent
            parent = self._parent(parent)

    def _enclosing_functions(self, comp: Component) -> Iterator[Component]:
        """The component and its enclosing functions, nearest first; a class body ends them."""
        for scope in (comp, *self._ancestors(comp)):
            if scope.type not in _SCOPE_TYPES:
                return
            yield scope

    def _mro(self, class_id: str, active: Tuple[str, ...] = ()) -> List[str]:
        """C3 linearization over the bases that resolve inside the repository."""
        if class_id in self._mro_cache:
            return self._mro_cache[class_id]
        if class_id in active:
            return [class_id]

        bases = self._class_bases(class_id)
        sequences = [
            [base] if base.startswith(_EXTERNAL) else self._mro(base, active + (class_id,))
            for base in bases
        ]
        merged = _c3_merge(sequences + [list(bases)])
        if merged is None:  # inconsistent hierarchy: use depth-first order
            merged = list(dict.fromkeys(k for seq in sequences for k in seq))

        result = [class_id] + merged
        self._mro_cache[class_id] = result
        return result

    def _class_bases(self, class_id: str) -> List[str]:
        """Base class ids in declaration order; a base outside the repository is a marker."""
        if class_id in self._class_bases_cache:
            return self._class_bases_cache[class_id]

        klass = self._by_id[class_id]
        node = self._definition_node(klass, "class_definition")
        superclasses = node.child_by_field_name("superclasses") if node is not None else None
        bases: List[str] = []
        for index, base_node in enumerate(superclasses.named_children if superclasses else []):
            if base_node.type == "subscript":  # Generic[T]
                base_node = base_node.child_by_field_name("value")
            if base_node is None or base_node.type not in ("identifier", "attribute"):
                continue  # keyword arguments such as metaclass=, and expressions

            dotted = base_node.text.decode("utf-8")
            if dotted.rsplit(".", 1)[-1] in _IGNORED_BASES:
                continue
            target = self._resolve_dotted(klass.file_path, dotted)
            is_class = target is not None and self._by_id[target].type == ComponentType.CLASS
            bases.append(target if is_class else f"{_EXTERNAL}:{class_id}:{index}")

        self._class_bases_cache[class_id] = bases
        return bases

    def _definition_node(self, comp: Component, node_type: str) -> Any:
        """The class or function node of a component, found by its first line."""
        definitions = self._module_index(comp.file_path).definitions
        return definitions.get((node_type, comp.line_number - 1))

    def _parameter_names(self, scope: Component) -> Set[str]:
        """Parameter names of a function, from the syntax tree.

        The tree is used because components of nested functions carry no parameters.
        """
        if scope.id in self._parameter_names_cache:
            return self._parameter_names_cache[scope.id]

        names: Set[str] = set()
        node = self._definition_node(scope, "function_definition")
        params_node = node.child_by_field_name("parameters") if node is not None else None
        for param in params_node.named_children if params_node is not None else []:
            if param.type == "identifier":
                name_node = param
            else:  # typed and default parameters, *args, **kwargs
                name_node = param.child_by_field_name("name") or next(
                    (child for child in param.named_children if child.type == "identifier"), None
                )
            if name_node is not None:
                names.add(name_node.text.decode("utf-8"))

        self._parameter_names_cache[scope.id] = names
        return names

    def _module_index(self, file_path: Path) -> _ModuleIndex:
        if file_path not in self._module_indexes:
            try:
                root = self._syntax_tree_for(file_path)
            except Exception:  # an unparsable file gives no definitions and no imports
                root = None
            self._module_indexes[file_path] = _ModuleIndex(root)
        return self._module_indexes[file_path]

    # --- bare names ---

    def _resolve_bare_name(self, caller: Component, name: str) -> Optional[str]:
        """Enclosing functions, then the module and its imports. Never a method."""
        for scope in self._enclosing_functions(caller):
            if name in self._parameter_names(scope):
                return None  # a parameter: the value is only known at run time
            nested = self._children.get(scope.id, {}).get(name)
            if nested is not None:
                return nested
        return self._resolve_symbol(caller.file_path, name, 0)

    # --- modules and imports ---

    def _resolve_dotted(self, file_path: Path, dotted: str) -> Optional[str]:
        """Resolve ``Name`` or ``module.Name`` as seen from one file."""
        if "." not in dotted:
            return self._resolve_symbol(file_path, dotted, 0)

        head, *middle, name = dotted.split(".")
        binding = self._module_index(file_path).imports.get(head)
        if binding is None:
            return None

        spec, imported = binding
        module_spec = _join(spec, imported) if imported else spec
        for part in middle:
            module_spec = _join(module_spec, part)
        module_file = self._module_file(file_path, module_spec)
        return self._resolve_symbol(module_file, name, 1) if module_file else None

    def _resolve_symbol(self, file_path: Path, name: str, depth: int) -> Optional[str]:
        """Top-level function or class ``name`` of a module, through its imports."""
        if depth > _MAX_IMPORT_DEPTH:
            return None

        local = self._top_level.get(file_path, {}).get(name)
        if local is not None:
            return local

        index = self._module_index(file_path)
        binding = index.imports.get(name)
        if binding is not None:
            spec, imported = binding
            if imported is None:
                return None  # a module object, not a callable
            module_file = self._module_file(file_path, spec)
            return self._resolve_symbol(module_file, imported, depth + 1) if module_file else None

        for spec in index.star_imports:
            module_file = self._module_file(file_path, spec)
            found = self._resolve_symbol(module_file, name, depth + 1) if module_file else None
            if found is not None:
                return found
        return None

    def _module_file(self, from_file: Path, spec: str) -> Optional[Path]:
        """File of a module spec, inside the analyzed tree. Relative specs start with dots."""
        # An absolute spec resolves the same way from every file.
        key = (from_file.parent if spec.startswith(".") else None, spec)
        if key not in self._module_files:
            self._module_files[key] = self._find_module_file(from_file, spec)
        return self._module_files[key]

    def _find_module_file(self, from_file: Path, spec: str) -> Optional[Path]:
        parts = [p for p in spec.lstrip(".").split(".") if p]
        if spec.startswith("."):
            base = from_file.parent
            for _ in range(len(spec) - len(spec.lstrip(".")) - 1):
                base = base.parent
            if not self._inside_root(base):
                return None
            roots = [base]
        elif parts and self._root is not None:
            roots = self._import_roots_for(parts[0])
        else:
            return None

        found = []
        for root in roots:
            candidate = self._module_candidate(root, parts)
            if candidate is not None and self._is_module_file(candidate):
                found.append(candidate)
        return found[0] if len(found) == 1 else None  # more than one match is ambiguous

    def _module_candidate(self, root: Path, parts: List[str]) -> Optional[Path]:
        """``root/a/b/__init__.py`` or ``root/a/b.py`` for the spec parts ``a.b``."""
        folder = root
        for part in parts[:-1]:
            if part not in self._folder_entries(folder):
                return None
            folder = folder / part

        entries = self._folder_entries(folder)
        package = folder.joinpath(*parts[-1:])
        # As in the import system, a package wins over a module of the same name.
        if (not parts or parts[-1] in entries) and "__init__.py" in self._folder_entries(package):
            return package / "__init__.py"
        if parts and f"{parts[-1]}.py" in entries:
            return folder / f"{parts[-1]}.py"
        return None

    def _import_roots_for(self, package: str) -> List[Path]:
        """Folders an absolute import of ``package`` can start from.

        A source root is a folder on the path of an analyzed file (the repository
        root, ``src/``, ...), or the parent of a folder with the package's name.
        """
        if package not in self._import_roots:
            folders = set(self._source_folders()) | self._package_parents.get(package, set())
            self._import_roots[package] = sorted(folders)
        return self._import_roots[package]

    def _source_folders(self) -> List[Path]:
        if self._source_folders_cache is None:
            root_depth = len(self._root.parts)
            self._source_folders_cache = sorted(
                {
                    Path(*file.parts[:depth])
                    for file in self._files
                    for depth in range(root_depth, len(file.parts))
                }
            )
        return self._source_folders_cache

    def _folder_entries(self, folder: Path) -> Set[str]:
        """Names in a folder, read once. The folders come from the paths of analyzed files."""
        if folder not in self._folder_entries_cache:
            try:
                with os.scandir(folder) as entries:
                    self._folder_entries_cache[folder] = {entry.name for entry in entries}
            except OSError:
                self._folder_entries_cache[folder] = set()
        return self._folder_entries_cache[folder]

    def _is_module_file(self, path: Path) -> bool:
        """An analyzed file, or a small regular file inside the analyzed tree (a re-export module)."""
        if path in self._any_component_in_file:
            return True

        try:
            is_small_file = path.is_file() and path.stat().st_size <= _MAX_UNINDEXED_MODULE_BYTES
        except OSError:
            return False
        return is_small_file and self._inside_root(path)

    def _inside_root(self, path: Path) -> bool:
        if self._root is None:
            return False
        try:
            return path.resolve().is_relative_to(self._root.resolve())
        except OSError:
            return False


def _bind_import(bindings: Dict[str, ImportBinding], item: Any, from_spec: Optional[str]) -> None:
    """Record one imported name. ``from_spec`` is None for ``import a.b``."""
    if item.type == "aliased_import":
        name = item.child_by_field_name("name").text.decode("utf-8")
        local = item.child_by_field_name("alias").text.decode("utf-8")
    else:
        name = item.text.decode("utf-8")
        local = name.split(".")[0] if from_spec is None else name

    if from_spec is None:
        bindings[local] = (name if item.type == "aliased_import" else local, None)
    else:
        bindings[local] = (from_spec, name)


def _join(spec: str, name: str) -> str:
    return spec + name if spec.strip(".") == "" else f"{spec}.{name}"


def _walk(root: Any) -> Iterator[Any]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _c3_merge(sequences: List[List[str]]) -> Optional[List[str]]:
    """Python's C3 merge. Returns None for an inconsistent hierarchy."""
    sequences = [list(seq) for seq in sequences if seq]
    result: List[str] = []
    while sequences:
        for seq in sequences:
            head = seq[0]
            if not any(head in other[1:] for other in sequences):
                break
        else:
            return None

        result.append(head)
        sequences = [[k for k in seq if k != head] for seq in sequences]
        sequences = [seq for seq in sequences if seq]
    return result
