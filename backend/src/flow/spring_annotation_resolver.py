"""Spring AOP adapter: link annotated Java methods to the in-repo advice for them.

An edge means "this advice may apply to this method" (see
``flow.annotation_edges``). Only a short allowlist of shapes is recognized;
any other pointcut, advice or aspect gives no edge, and missing edges are
normal.

Handler side. A method of a concrete class carrying AspectJ's ``@Aspect``
(default per-clause), with exactly one of ``@Around``, ``@Before``,
``@After``, ``@AfterReturning``, ``@AfterThrowing``, whose pointcut
(``value`` or ``pointcut``, not both) is a string literal, or a ``+``
concatenation of literals, in one of the shapes of ``flow.pointcut_shapes``:

- ``@annotation(com.x.Audit)`` or ``execution(@com.x.Audit * *(..))``:
  methods annotated with ``@Audit``;
- ``@annotation(audit)``, ``audit`` being the advice parameter that binds it,
  declared as an annotation type;
- ``@within(com.x.Audit)``: the methods of classes annotated with ``@Audit``;
- ``name()``: a parameterless ``@Pointcut`` method of the same class, when
  every ``@Pointcut`` method of that class is itself accepted;
- a top-level ``||`` of these.

Every advice parameter must be bound: a leading ``JoinPoint`` /
``ProceedingJoinPoint`` (``@Around`` only) / ``JoinPoint.StaticPart``, the
``returning`` / ``throwing`` name, or the one ``@annotation(param)``
parameter (names from ``argNames`` when given, else as declared). The
``returning`` parameter must be declared as ``Object``: Spring skips the
advice for a return value its type does not accept (``void`` included).

Refused, among everything else: ``!``, ``&&``, Spring's word operators,
escape sequences, text blocks, constants, simple type names inside the
pointcut (AspectJ resolves them without the file's imports), references to
pointcuts of other classes, a pointcut naming an in-repo type that is not a
RUNTIME-retained annotation, unbound advice parameters, several AspectJ
annotations on one method, per-clauses, abstract aspects, aspects with a
supertype (Spring would also read advice annotations declared there) and
aspects another class extends (Spring would evaluate their advice in the
subclass's scope).

Annotated side. A method the adapter takes as advisable
(``java_declarations.advisable_methods``) gets an edge for each of its
annotations, or its class's for ``@within``, that resolves to the in-repo
``@interface`` an advice names, with ``@Retention(RetentionPolicy.RUNTIME)``.
"""

import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from ..core.models import Component
from .java_declarations import (
    JavaClass,
    JavaFile,
    JavaMethod,
    TypeIndex,
    advisable_methods,
    denotes,
    read_java_files,
    resolve_annotation,
    string_attributes,
)
from .pointcut_shapes import ANNOTATION, EXECUTION_ANNOTATED, REFERENCE, WITHIN, parse_pointcut

_ASPECTJ = "org.aspectj.lang.annotation."
_ADVICE = frozenset({"Around", "Before", "After", "AfterReturning", "AfterThrowing"})
_ASPECTJ_NAMES = _ADVICE | {"Pointcut", "Aspect"}
_ATTRIBUTES = frozenset({"value", "pointcut", "argNames", "returning", "throwing"})
_JOIN_POINTS = {
    "org.aspectj.lang.JoinPoint": "JoinPoint",
    "org.aspectj.lang.ProceedingJoinPoint": "ProceedingJoinPoint",
    "org.aspectj.lang.JoinPoint.StaticPart": "StaticPart",
}
# Supertypes an aspect may have: none declares methods with advice annotations.
_PLAIN_SUPERTYPES = frozenset({"Object", "Serializable", "Cloneable", "Ordered", "PriorityOrdered"})
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_METHOD, _CLASS = "method", "class"  # what an advice reference matches
_MAX_NESTING = 32  # named pointcuts referencing named pointcuts

Leaves = Tuple[Any, ...]
References = Set[Tuple[str, str]]  # (annotation FQN, _METHOD or _CLASS)


def resolve_spring_annotation_edges(
    components: Sequence[Component], get_tree: Callable[[Component], Any]
) -> List[Tuple[str, str, int]]:
    """``(annotated_method_id, advice_method_id, line)`` for Java components."""
    files, index, methods = read_java_files(components, get_tree)
    if not index.runtime_annotations:
        return []
    extended = {_simple(name) for f in files for cls in f.classes for name in cls.supertypes}
    handlers: Dict[str, List[Tuple[str, str]]] = {}  # annotation FQN -> [(scope, advice id)]
    for f in files:
        for cls in f.classes:
            if _is_aspect(cls, f, index, extended):
                for fqn, scope, advice_id in _advice_references(cls, f, index, methods):
                    handlers.setdefault(fqn, []).append((scope, advice_id))
    edges = set()
    for f, cls, targets in advisable_methods(files, index, methods) if handlers else ():
        for method, method_id in targets:
            for annotation in method.annotations:
                fqn = resolve_annotation(annotation.written, f, index) or ""
                for scope, advice_id in handlers.get(fqn, ()):
                    if scope == _METHOD:
                        edges.add((method_id, advice_id, annotation.line))
        for annotation in cls.annotations:
            fqn = resolve_annotation(annotation.written, f, index) or ""
            for scope, advice_id in handlers.get(fqn, ()):
                if scope == _CLASS:
                    edges.update(
                        (method_id, advice_id, annotation.line) for _, method_id in targets
                    )
    return sorted(edges)


def _is_aspect(cls: JavaClass, f: JavaFile, index: TypeIndex, extended: Set[str]) -> bool:
    """A concrete, unextended class carrying AspectJ's ``@Aspect`` with no
    per-clause, no other AspectJ type annotation and no supertype that may
    carry advice annotations."""
    aspects = [a for a in cls.annotations if _simple(a.written) == "Aspect"]
    if (
        cls.kind != "class_declaration"
        or not cls.clean
        or "abstract" in cls.modifiers
        or len(aspects) != 1
        or cls.name in extended
        or any(_simple(name) not in _PLAIN_SUPERTYPES for name in cls.supertypes)
        or any(_may_be_aspectj(a.written, f) for a in cls.annotations if a is not aspects[0])
    ):
        return False
    attributes = string_attributes(aspects[0].node)
    return attributes in ({}, {"value": ""}) and denotes(
        aspects[0].written, _ASPECTJ + "Aspect", f, index
    )


def _may_be_aspectj(written: str, f: JavaFile) -> bool:
    """True when an annotation name may come from ``org.aspectj.lang.annotation``."""
    first = written.partition(".")[0]
    return f.imports.get(first, written).startswith("org.aspectj.") or (
        "." not in written and first not in f.imports and _ASPECTJ[:-1] in f.on_demand
    )


def _aspectj_annotation(
    method: JavaMethod, f: JavaFile, index: TypeIndex
) -> Optional[Tuple[str, Dict[str, str]]]:
    """(kind, string attributes) of a method's one AspectJ annotation, or None
    for none, several, or one whose attributes cannot be read."""
    named = [a for a in method.annotations if _simple(a.written) in _ASPECTJ_NAMES]
    if len(named) != 1 or not method.clean:
        return None
    kind = _simple(named[0].written)
    attributes = string_attributes(named[0].node)
    if attributes is None or not set(attributes) <= _ATTRIBUTES:
        return None
    return (kind, attributes) if denotes(named[0].written, _ASPECTJ + kind, f, index) else None


def _expression(attributes: Dict[str, str]) -> Optional[str]:
    if ("value" in attributes) == ("pointcut" in attributes):
        return None  # missing, or both given
    return attributes.get("pointcut", attributes.get("value"))


def _advice_references(
    cls: JavaClass, f: JavaFile, index: TypeIndex, methods: Dict[Tuple[str, int], str]
) -> Iterator[Tuple[str, str, str]]:
    """(annotation FQN, scope, advice method id) for each advice of an aspect."""
    pointcuts = _named_pointcuts(cls, f, index)
    for method in cls.methods:
        found = _aspectj_annotation(method, f, index)
        advice_id = methods.get((f.path, method.line))
        if found is None or found[0] not in _ADVICE or advice_id is None:
            continue
        expression = _expression(found[1])
        formals = _formals(method, found[0], found[1], f, index)
        if expression is None or formals is None:
            continue
        for fqn, scope in sorted(_references(expression, formals, pointcuts, f, index) or ()):
            yield fqn, scope, advice_id


def _named_pointcuts(cls: JavaClass, f: JavaFile, index: TypeIndex) -> Optional[Dict[str, Leaves]]:
    """Leaves of each ``@Pointcut`` method of an aspect, by method name.

    None when any of them is refused: AspectJ resolves every named pointcut
    of the class on the first reference, so one it rejects would fail the
    advice that references another.
    """
    pointcuts: Dict[str, Leaves] = {}
    for method in cls.methods:
        if not any(_simple(a.written) == "Pointcut" for a in method.annotations):
            continue
        found = _aspectj_annotation(method, f, index)
        if found is None or found[0] != "Pointcut" or method.params:
            return None
        expression = _expression(found[1])
        leaves = parse_pointcut(expression) if expression is not None else None
        if leaves is None or method.name in pointcuts:
            return None
        pointcuts[method.name] = leaves
    for leaves in pointcuts.values():
        if _expand(leaves, pointcuts, index, ()) is None:
            return None
    return pointcuts


def _references(
    expression: str,
    formals: Dict[str, str],
    pointcuts: Optional[Dict[str, Leaves]],
    f: JavaFile,
    index: TypeIndex,
) -> Optional[References]:
    """What an advice expression links; None when refused."""
    leaves = parse_pointcut(expression)
    if leaves is None:
        return None
    if not formals:
        return _expand(leaves, pointcuts, index, ())
    # A bound parameter: only the plain binding form `@annotation(param)`.
    if len(formals) != 1 or len(leaves) != 1:
        return None
    ((name, written_type),) = formals.items()
    if leaves[0].kind != ANNOTATION or leaves[0].name != name:
        return None
    fqn = resolve_annotation(written_type, f, index)
    return {(fqn, _METHOD)} if fqn else None


def _expand(
    leaves: Leaves,
    pointcuts: Optional[Dict[str, Leaves]],
    index: TypeIndex,
    resolving: Tuple[str, ...],
) -> Optional[References]:
    """What leaves that bind nothing link; None when refused."""
    refs: References = set()
    for leaf in leaves:
        if leaf.kind == REFERENCE:
            if pointcuts is None or leaf.name not in pointcuts or leaf.name in resolving:
                return None  # another class's, inherited, undefined, circular, or refused
            if len(resolving) >= _MAX_NESTING:
                return None
            inner = _expand(pointcuts[leaf.name], pointcuts, index, resolving + (leaf.name,))
            if inner is None:
                return None
            refs |= inner
        elif leaf.kind in (ANNOTATION, WITHIN, EXECUTION_ANNOTATED):
            if "." not in leaf.name:
                return None  # a simple type name: AspectJ ignores the file's imports
            if leaf.name in index.runtime_annotations:
                refs.add((leaf.name, _CLASS if leaf.kind == WITHIN else _METHOD))
            elif leaf.name in index.types:
                return None  # an in-repo type that is no RUNTIME annotation
            # Otherwise a type outside the repository: it links nothing.
        else:
            return None  # a shape this adapter does not handle
    return refs


def _formals(
    method: JavaMethod, advice: str, attributes: Dict[str, str], f: JavaFile, index: TypeIndex
) -> Optional[Dict[str, str]]:
    """Advice parameters the pointcut must bind (name -> type as written), or
    None when some parameter is bound in a way this adapter does not model."""
    params = method.params
    leading = _join_point(params[0][1], f, index) if params else None
    if leading == "ProceedingJoinPoint" and advice != "Around":
        return None
    offset = 1 if leading else 0
    names = [name for name, _ in params]
    if attributes.get("argNames", "").strip():
        given = [name.strip(" \t\r\n") for name in attributes["argNames"].split(",")]
        if not all(_IDENT.fullmatch(name) for name in given):
            return None
        if len(given) == len(params) - offset:
            names = names[:offset] + given
        elif len(given) == len(params):
            names = given
        else:
            return None
    rest = list(zip(names[offset:], (written for _, written in params[offset:])))
    rest_names = [name for name, _ in rest]
    if not all(rest_names) or len(set(rest_names)) != len(rest_names):
        return None
    bound = {attributes.get(key, "") for key in ("returning", "throwing")} - {""}
    if not bound <= set(rest_names):
        return None
    returning = attributes.get("returning", "")
    if returning and not denotes(dict(rest)[returning], "java.lang.Object", f, index):
        return None  # a narrower type: Spring skips the advice for other return values
    return {name: written for name, written in rest if name not in bound}


def _join_point(written: str, f: JavaFile, index: TypeIndex) -> Optional[str]:
    """Which AspectJ join-point type a parameter type as written denotes, if any."""
    for fqn, kind in _JOIN_POINTS.items():
        if denotes(written, fqn, f, index):
            return kind
    if written == "JoinPoint.StaticPart" and denotes(
        "JoinPoint", "org.aspectj.lang.JoinPoint", f, index
    ):
        return "StaticPart"
    return None


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]
