"""MCP text output must show the id whenever the display name differs from it."""

from pathlib import Path

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, ComponentType
from src.mcp.tools.query_tools import (
    _format_component,
    _format_component_detail,
    _format_component_list,
)

SHORT_ID = "space-a::module.Widget_a1b2"


def _component(component_id: str, display_name: str | None = None) -> Component:
    component = Component(
        id=component_id,
        type=ComponentType.FUNCTION,
        file_path=Path("module.py"),
        line_number=1,
        end_line_number=2,
        language="python",
    )
    component._name = display_name
    return component


def test_list_shows_id_for_short_named_component():
    out = _format_component_list([_component(SHORT_ID, "Widget").to_dict()])
    assert f"Widget [id: {SHORT_ID}]" in out


def test_single_and_detail_show_id_for_short_named_component():
    comp = _component(SHORT_ID, "Widget").to_dict()
    assert f"Component: Widget [id: {SHORT_ID}]" in _format_component(comp)
    assert f"Component: Widget [id: {SHORT_ID}]" in _format_component_detail(comp)


def test_output_unchanged_when_name_is_id():
    out = _format_component_list([_component("space-a::module.Gadget").to_dict()])
    assert "space-a::module.Gadget (function)" in out
    assert "[id:" not in out


def test_printed_id_resolves_in_follow_up_lookup():
    catalog = ComponentCatalog()
    catalog.add_component(_component(SHORT_ID, "Widget"))
    results = [c.to_dict() for c in catalog.search_components("space-a::*Widget*")]
    out = _format_component_list(results)
    printed_id = out.split("[id: ", 1)[1].split("]", 1)[0]
    assert catalog.find_component(printed_id).id == SHORT_ID
