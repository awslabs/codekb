"""Implicit annotation-handler edges: the language-agnostic contract.

Frameworks run code for an annotation (or decorator, attribute) without any
call in the annotated method's body: a Spring aspect's advice for
``@Around("@annotation(X)")``, a Python decorator, a NestJS guard. CodeKB
records such a link as a typed call-graph edge from the annotated method to
the in-repo handler, ``(handler_id, line, EDGE_KIND_ANNOTATION)``, which the
flow tools label as implicit and never present as a direct call.

Contract for a language adapter:

- It is a callable ``adapter(components, get_tree)`` that receives the
  components of one language and ``get_tree(component)``, which returns the
  parsed tree-sitter root of a component's file (shared with the call-graph
  pass, so files are not re-parsed).
- It returns ``(annotated_method_id, handler_id, line)`` triples, ``line``
  being the annotation usage's line in the annotated method's file. Nothing
  else: de-duplication, ordering and the edge format are done here.
- An edge means "this handler may apply to this method". An adapter emits a
  triple only for shapes it recognizes and refuses everything else; a
  missing edge is normal and never an error.

Register an adapter in ``_adapters`` with the ``Component.language`` it
serves and the edge kind it emits (``EDGE_KIND_ANNOTATION`` by default); a
language may have several adapters (Spring annotations today, Spring XML
configuration later, which may emit a sibling kind). An adapter that raises
loses its edges for that run (logged), never the call graph.
"""

from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Sequence, Tuple

from ..core.logging_utils import get_logger
from ..core.models import EDGE_KIND_ANNOTATION, Component

logger = get_logger(__name__)

AnnotationEdge = Tuple[str, str, int]  # (annotated_method_id, handler_id, line)
AnnotationEdgeAdapter = Callable[
    [Sequence[Component], Callable[[Component], Any]], Iterable[AnnotationEdge]
]


class Adapter(NamedTuple):
    language: str  # the Component.language it serves
    resolve: AnnotationEdgeAdapter
    kind: str = EDGE_KIND_ANNOTATION  # the kind of the edges it emits


def _adapters() -> List[Adapter]:
    """The registered adapters, imported lazily."""
    from .spring_annotation_resolver import resolve_spring_annotation_edges

    return [Adapter("java", resolve_spring_annotation_edges)]


def resolve_annotation_edges(
    components: Sequence[Component], get_tree: Callable[[Component], Any]
) -> Dict[str, List[Tuple[str, int, str]]]:
    """Run every adapter and return ``{method_id: [(handler_id, line, kind), ...]}``.

    One edge per (method, handler, kind), at the first usage line; self-links
    are dropped. Sources and edges are sorted, so the result does not depend
    on component order.
    """
    lines: Dict[Tuple[str, str, str], int] = {}
    for adapter in _adapters():
        scoped = [c for c in components if c.language == adapter.language]
        if not scoped:
            continue
        try:
            triples = list(adapter.resolve(scoped, get_tree))
        except Exception as exc:
            logger.warning(f"Annotation-handler adapter for {adapter.language} failed: {exc}")
            continue
        for method_id, handler_id, line in triples:
            if method_id == handler_id:
                continue
            key = (method_id, handler_id, adapter.kind)
            if key not in lines or line < lines[key]:
                lines[key] = line
    edges: Dict[str, List[Tuple[str, int, str]]] = {}
    for (method_id, handler_id, kind), line in sorted(lines.items()):
        edges.setdefault(method_id, []).append((handler_id, line, kind))
    return edges
