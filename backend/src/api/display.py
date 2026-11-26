"""UI-only ``display_name`` helpers for the REST layer.

``display_name`` is a human-friendly, parent-qualified label derived from a
component id. It is attached in the REST routes and NOWHERE at or below the
shared service: the service, the MCP tools, the in-app agent and the persisted
knowledge base never carry it. The reason is strict — an LLM driving the MCP
tools must not key tool chaining on it, because the label is not unique and is
not a valid lookup key. Only ``id`` is the key. These helpers live in the api
layer, and are imported only by REST route modules, so the enrichment cannot
leak into a service response.
"""

from collections.abc import Iterable

from ..core.models import derive_display_name


def display_name_for(component: dict) -> str:
    """Return the UI-only display label for a serialized component dict."""
    attributes = component.get("attributes") or {}
    return derive_display_name(
        component.get("id", ""),
        component.get("language", ""),
        parent_component=component.get("parent_component"),
        accessor_kind=attributes.get("accessor_kind"),
    )


def with_display_name(component: dict) -> dict:
    """Attach ``display_name`` to a component dict in place and return it."""
    component["display_name"] = display_name_for(component)
    return component


def display_names_for_ids(ids: Iterable[str], meta_by_id: dict[str, dict]) -> dict[str, str]:
    """Build an id -> display_name lookup for referenced component ids.

    Flow and dependency responses reference components by bare id string, so the
    label is supplied as a sibling lookup rather than attached to the string.
    Metadata (language / parent / accessor kind) comes from the space's own
    component list so Terraform and Dart accessor ids render exactly as they do
    everywhere else; an id absent from the catalog falls back to id-only
    derivation.
    """
    names: dict[str, str] = {}
    for cid in ids:
        if not cid or cid in names:
            continue
        meta = meta_by_id.get(cid)
        names[cid] = display_name_for(meta) if meta is not None else derive_display_name(cid, "")
    return names


def attach_search_display_names(
    results: list[dict], meta_by_id: dict[str, dict]
) -> list[dict]:
    """Attach ``display_name`` to vector-store search results in place.

    A search result carries its component id under ``key`` (not ``id``) and no
    component fields, so the label is resolved from ``meta_by_id`` when the id
    is present there and falls back to id-only derivation otherwise. ``id``
    stays the lookup key; ``display_name`` is render-only.
    """
    names = display_names_for_ids((r.get("key", "") for r in results), meta_by_id)
    for result in results:
        result["display_name"] = names.get(result.get("key", ""), "")
    return results
