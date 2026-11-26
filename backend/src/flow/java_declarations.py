"""Java declarations as the Spring annotation-handler adapters read them.

``read_java_files`` turns each Java file's syntax tree into the facts the
adapters use (package, imports, classes, methods, annotations) and indexes
what the repository declares. Names resolve as javac resolves them in the
common cases only: written fully qualified, an explicit single-type import,
the file's own package, or a unique in-repo type among the on-demand imports.
Where the repository alone cannot settle a name (a single static import, or
another nested type of that simple name that may be a member type in scope)
the answer is None, never a guess. ``advisable_methods`` picks, on the
annotated side, the methods Spring's proxy-based AOP may advise.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from ..core.logging_utils import get_logger
from ..core.models import Component, ComponentType

logger = get_logger(__name__)

_TYPE_NODES = frozenset(
    "class_declaration interface_declaration enum_declaration record_declaration"
    " annotation_type_declaration".split()
)
_COMMENTS = ("line_comment", "block_comment")
_GENERICS = re.compile(r"<[^<>]*>")
_RETENTION = "java.lang.annotation.Retention"
_RETENTION_POLICY = "java.lang.annotation.RetentionPolicy"
# Spring never proxies aspects, AOP infrastructure beans or the post-processors
# it creates before auto-proxying starts, nor their subclasses.
_UNPROXIED_PACKAGES = ("org.aopalliance.", "org.springframework.aop.")
_UNPROXIED_SUFFIXES = ("PostProcessor", "Advisor")
_UNPROXIED_SUPERTYPES = frozenset(
    "Advice Interceptor MethodInterceptor ConstructorInterceptor MethodBeforeAdvice"
    " AfterReturningAdvice ThrowsAdvice Pointcut AopInfrastructureBean".split()
)


@dataclass
class JavaAnnotation:
    written: str  # the name as written: "Log", "com.x.Log"
    line: int
    node: Any


@dataclass
class JavaMethod:
    name: str
    line: int  # 1-based start line, == Component.line_number
    modifiers: Set[str]
    annotations: List[JavaAnnotation]
    params: List[Tuple[str, str]]  # (name, type as written; "" for arrays/varargs)
    clean: bool  # the header parsed without errors


@dataclass
class JavaClass:
    name: str
    kind: str  # tree-sitter node type
    modifiers: Set[str]
    annotations: List[JavaAnnotation]
    supertypes: List[str]  # as written, generics dropped, an imported first segment expanded
    methods: List[JavaMethod]
    clean: bool


@dataclass
class JavaFile:
    path: str
    package: str = ""
    imports: Dict[str, str] = field(default_factory=dict)  # simple name -> FQN
    on_demand: List[str] = field(default_factory=list)
    static_imports: Set[str] = field(default_factory=set)  # full names
    static_on_demand: List[str] = field(default_factory=list)
    classes: List[JavaClass] = field(default_factory=list)
    types: List[str] = field(default_factory=list)  # local paths ("Outer.Inner")
    annotation_types: Dict[str, List[JavaAnnotation]] = field(default_factory=dict)

    @property
    def static_names(self) -> Set[str]:
        return {_simple(path) for path in self.static_imports}


@dataclass
class TypeIndex:
    types: Set[str] = field(default_factory=set)  # FQNs, nested types as p.Outer.Inner
    nested: Dict[str, Set[str]] = field(default_factory=dict)  # simple name -> FQNs
    runtime_annotations: Set[str] = field(default_factory=set)  # @Retention(RUNTIME)


def read_java_files(
    components: Sequence[Component], get_tree: Callable[[Component], Any]
) -> Tuple[List[JavaFile], TypeIndex, Dict[Tuple[str, int], str]]:
    """The files of the components, the type index, and method ids by (file, line).

    A file whose tree cannot be read is skipped: it loses its own edges and
    its declarations take no part in resolving names.
    """
    first_by_file: Dict[str, Component] = {}
    methods: Dict[Tuple[str, int], str] = {}
    for comp in components:
        first_by_file.setdefault(str(comp.file_path), comp)
        if comp.type == ComponentType.METHOD:
            methods[(str(comp.file_path), comp.line_number)] = comp.id
    files = []
    for path in sorted(first_by_file):
        try:
            root = get_tree(first_by_file[path])
            if root is not None:
                files.append(_read_file(path, root))
        except Exception as exc:
            logger.debug(f"Annotation adapter skipped {path}: {exc}")
    index = TypeIndex()
    for f in files:
        for path in f.types:
            index.types.add(_qualify(f.package, path))
            if "." in path:
                index.nested.setdefault(_simple(path), set()).add(_qualify(f.package, path))
    index.runtime_annotations = {
        _qualify(f.package, path)
        for f in files
        for path, annotations in f.annotation_types.items()
        if _is_runtime_retained(path, annotations, f, index)
    }
    return files, index, methods


# ── names ────────────────────────────────────────────────────────────────


def resolve_type(written: str, f: JavaFile, index: TypeIndex) -> Optional[str]:
    """The FQN javac gives a type name written in ``f``; None when the
    repository cannot settle it (a dotted name nothing resolves is taken as
    fully qualified)."""
    first, dot, rest = written.partition(".")
    if first in f.static_names:
        return None  # a static import may bring in a member type of that name
    candidates = {f"{prefix}.{first}" for prefix in f.on_demand} & index.types
    if first in f.imports:
        resolved: Optional[str] = f.imports[first]
    elif _qualify(f.package, first) in index.types:
        resolved = _qualify(f.package, first)
    elif len(candidates) > 1:
        return None
    else:
        resolved = candidates.pop() if candidates else None
    if index.nested.get(first, {resolved}) != {resolved}:
        return None  # another type of that name may be a member type in scope
    if resolved is None:
        return written if dot else None
    return resolved + dot + rest


def resolve_annotation(written: str, f: JavaFile, index: TypeIndex) -> Optional[str]:
    """The in-repo RUNTIME annotation a name written in ``f`` denotes, or None."""
    fqn = resolve_type(written, f, index)
    return fqn if fqn in index.runtime_annotations else None


def denotes(written: str, expected: str, f: JavaFile, index: TypeIndex) -> bool:
    """True when a name written in ``f`` denotes the library type ``expected``
    (a ``java.lang`` type needs no import)."""
    if expected in index.types:
        return False  # the repository declares that name itself
    if written == expected:
        return True
    package, _, simple = expected.rpartition(".")
    if written != simple or simple in index.nested or simple in f.static_names:
        return False
    if simple in f.imports:
        return f.imports[simple] == expected
    if _qualify(f.package, simple) in index.types:
        return False
    if any(f"{prefix}.{simple}" in index.types for prefix in f.on_demand):
        return False
    return package in f.on_demand or package == "java.lang"


def _is_runtime_retained(
    path: str, annotations: List[JavaAnnotation], f: JavaFile, index: TypeIndex
) -> bool:
    """True when an ``@interface`` carries ``@Retention(RetentionPolicy.RUNTIME)``."""
    retentions = [a for a in annotations if _simple(a.written) == "Retention"]
    if len(retentions) != 1 or not denotes(retentions[0].written, _RETENTION, f, index):
        return False
    arguments = _arguments(retentions[0].node) or [("", None)]
    key, value = arguments[0]
    if len(arguments) != 1 or key != "value" or value is None:
        return False
    if value.type not in ("identifier", "field_access", "scoped_identifier"):
        return False
    owner, _, constant = re.sub(r"\s+", "", _text(value)).rpartition(".")
    if owner:
        return constant == "RUNTIME" and denotes(owner, _RETENTION_POLICY, f, index)
    # A bare RUNTIME from a static import; a nested @interface may see a field instead.
    return (
        constant == "RUNTIME"
        and "." not in path
        and (
            f"{_RETENTION_POLICY}.RUNTIME" in f.static_imports
            or ("RUNTIME" not in f.static_names and _RETENTION_POLICY in f.static_on_demand)
        )
    )


# ── the annotated side ───────────────────────────────────────────────────


def advisable_methods(
    files: Sequence[JavaFile], index: TypeIndex, methods: Dict[Tuple[str, int], str]
) -> Iterator[Tuple[JavaFile, JavaClass, List[Tuple[JavaMethod, str]]]]:
    """Per class, the methods (with component ids) the adapters may link.

    Spring's proxies never advise private or static methods, and Spring
    never auto-proxies aspects (an ``@Aspect`` class, or one carrying an
    in-repo annotation that carries ``@Aspect``), AOP infrastructure or
    post-processors, nor their in-repo subclasses (all recognized by simple
    name). Also left out: final methods and final classes (enums and records
    included), which the CGLIB proxy, Spring Boot's default, cannot advise (a
    JDK interface proxy could, but which proxy applies is not visible here);
    abstract methods and interface methods; declarations whose header did not
    parse.
    """
    aspect_annotations = {"Aspect"} | {
        _simple(path)
        for f in files
        for path, annotations in f.annotation_types.items()
        if any(_simple(a.written) == "Aspect" for a in annotations)
    }
    classes = [cls for f in files for cls in f.classes]
    unproxied = {
        cls.name
        for cls in classes
        if any(_simple(a.written) in aspect_annotations for a in cls.annotations)
        or any(
            name.startswith(_UNPROXIED_PACKAGES)
            or _simple(name) in _UNPROXIED_SUPERTYPES
            or _simple(name).endswith(_UNPROXIED_SUFFIXES)
            for name in cls.supertypes
        )
    }
    grew = True
    while grew:  # their in-repo subclasses, transitively
        grew = False
        for cls in classes:
            if cls.name not in unproxied and unproxied & {_simple(n) for n in cls.supertypes}:
                unproxied.add(cls.name)
                grew = True
    for f in files:
        for cls in f.classes:
            if cls.kind != "class_declaration" or "final" in cls.modifiers or not cls.clean:
                continue
            if cls.name in unproxied:
                continue
            yield (
                f,
                cls,
                [
                    (method, methods[(f.path, method.line)])
                    for method in cls.methods
                    if (f.path, method.line) in methods
                    and method.clean
                    and not method.modifiers & {"private", "static", "final", "abstract"}
                ],
            )


# ── reading the syntax tree ──────────────────────────────────────────────


def _read_file(path: str, root: Any) -> JavaFile:
    f = JavaFile(path=path)
    for child in root.children:
        names = [_text(c) for c in child.children if c.type in ("identifier", "scoped_identifier")]
        if child.type == "package_declaration" and names:
            f.package = names[0]
        elif child.type == "import_declaration" and names:
            on_demand = any(c.type == "asterisk" for c in child.children)
            if any(c.type == "static" for c in child.children):
                (f.static_on_demand.append if on_demand else f.static_imports.add)(names[0])
            elif on_demand:
                f.on_demand.append(names[0])
            else:
                f.imports[_simple(names[0])] = names[0]
    pending: List[Tuple[Any, str]] = [(root, "")]
    while pending:  # iterative: deep files do not exhaust the recursion limit
        node, scope = pending.pop()
        if node.type in _TYPE_NODES:
            name = next((_text(c) for c in node.children if c.type == "identifier"), "")
            if name:
                scope = f"{scope}.{name}" if scope else name
                f.types.append(scope)
                if node.type == "annotation_type_declaration":
                    f.annotation_types[scope] = _annotations(node)
                else:
                    f.classes.append(_read_class(node, name, f))
        pending.extend((child, scope) for child in reversed(node.children))
    return f


def _read_class(node: Any, name: str, f: JavaFile) -> JavaClass:
    supertypes = []
    for child in node.children:
        if child.type in ("superclass", "super_interfaces", "extends_interfaces"):
            text = re.sub(r"\s+", " ", _text(child)).split(" ", 1)[-1]  # drop the keyword
            while _GENERICS.search(text):
                text = _GENERICS.sub("", text)
            for written in (part.replace(" ", "") for part in text.split(",") if part.strip()):
                first, dot, rest = written.partition(".")
                supertypes.append(f.imports[first] + dot + rest if first in f.imports else written)
    cls = JavaClass(
        name, node.type, _modifiers(node), _annotations(node), supertypes, [], _clean(node)
    )
    body = node.child_by_field_name("body")
    members = list(body.children) if body is not None else []
    for member in list(members):
        if member.type == "enum_body_declarations":
            members.extend(member.children)
    for member in members:
        if member.type == "method_declaration":
            method_name = member.child_by_field_name("name")
            cls.methods.append(
                JavaMethod(
                    _text(method_name) if method_name is not None else "",
                    member.start_point[0] + 1,
                    _modifiers(member),
                    _annotations(member),
                    _parameters(member),
                    _clean(member),
                )
            )
    return cls


def _clean(node: Any) -> bool:
    """True when a declaration's header (all but its body), and any text just
    before it that the parser could not attach, parsed without errors."""
    body = node.child_by_field_name("body")
    if any(c.has_error or c.is_missing for c in node.children if c != body):
        return False
    sibling = node.prev_sibling
    while sibling is not None and sibling.type in (*_COMMENTS, "ERROR"):
        if sibling.type == "ERROR":
            return False
        sibling = sibling.prev_sibling
    return True


def _parameters(method: Any) -> List[Tuple[str, str]]:
    params: List[Tuple[str, str]] = []
    formal = method.child_by_field_name("parameters")
    for param in formal.named_children if formal is not None else ():
        if param.type == "formal_parameter":
            name = param.child_by_field_name("name")
            type_node = param.child_by_field_name("type")
            is_array = param.child_by_field_name("dimensions") is not None or (
                type_node is not None and type_node.type == "array_type"
            )
            written = "" if is_array or type_node is None else re.sub(r"\s+", "", _text(type_node))
            params.append((_text(name) if name is not None else "", written))
        elif param.type in ("spread_parameter", "receiver_parameter"):
            params.append(("", ""))  # varargs or `this`: never bound
    return params


def _modifiers(node: Any) -> Set[str]:
    return {
        keyword.type
        for child in node.children
        if child.type == "modifiers"
        for keyword in child.children
        if not keyword.is_named
    }


def _annotations(node: Any) -> List[JavaAnnotation]:
    found = []
    for child in node.children:
        for mod in child.children if child.type == "modifiers" else ():
            name = mod.child_by_field_name("name")
            if mod.type in ("marker_annotation", "annotation") and name is not None:
                written = re.sub(r"\s+", "", _text(name))
                found.append(JavaAnnotation(written, mod.start_point[0] + 1, mod))
    return found


def _arguments(annotation_node: Any) -> List[Tuple[str, Any]]:
    """(element name, value node) of an annotation's arguments; ``value`` for
    the single-element shorthand."""
    args = annotation_node.child_by_field_name("arguments")
    pairs = []
    for child in args.named_children if args is not None else ():
        if child.type == "element_value_pair":
            key = child.child_by_field_name("key")
            pairs.append(
                (_text(key) if key is not None else "", child.child_by_field_name("value"))
            )
        elif child.type not in _COMMENTS:
            pairs.append(("value", child))
    return pairs


def string_attributes(annotation_node: Any) -> Optional[Dict[str, str]]:
    """An annotation's elements as strings; None when one is repeated or is not
    a string literal or a ``+`` concatenation of them."""
    values: Dict[str, str] = {}
    for key, value in _arguments(annotation_node):
        text = _literal(value)
        if text is None or key in values:
            return None
        values[key] = text
    return values


def _literal(node: Any) -> Optional[str]:
    """The text of a string literal or a ``+`` concatenation of them, or None
    for anything else: escape sequences, text blocks, names, calls."""
    if node is None:
        return None
    if node.type == "string_literal":
        parts = []
        for child in node.children:
            if child.type == "string_fragment":
                parts.append(_text(child))
            elif child.type != '"':
                return None
        return "".join(parts)
    operands = [c for c in node.named_children if c.type not in _COMMENTS]
    if node.type == "parenthesized_expression" and len(operands) == 1:
        return _literal(operands[0])
    operators = {_text(c) for c in node.children if not c.is_named}
    if node.type == "binary_expression" and operators == {"+"} and len(operands) == 2:
        left, right = _literal(operands[0]), _literal(operands[1])
        return None if left is None or right is None else left + right
    return None


def _qualify(package: str, name: str) -> str:
    return f"{package}.{name}" if package else name


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _text(node: Any) -> str:
    return node.text.decode("utf-8", "replace") if node.text is not None else ""
