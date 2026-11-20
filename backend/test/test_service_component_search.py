"""Tests for the component-type filter on CodeKBService.search_components.

The Syntactic Search UI sends ?pattern= and ?type= together. Before this fix
the route dropped ?type= whenever a pattern was present, so selecting
"Variables" still returned classes. These tests pin the combined behaviour.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, ComponentType
from src.service.codekb_service import CodeKBService


def _component(name: str, comp_type: ComponentType) -> Component:
    return Component(
        id=name,
        type=comp_type,
        file_path=Path("synthetic.cs"),
        line_number=1,
        end_line_number=2,
        language="csharp",
    )


@pytest.fixture
def codekb_service(monkeypatch) -> CodeKBService:
    """A codekb_service whose only space holds a fixed mix of component types."""
    catalog = ComponentCatalog()
    catalog.add_component(_component("Ping", ComponentType.CLASS))
    catalog.add_component(_component("Pong", ComponentType.CLASS))
    catalog.add_component(_component("PingHandler", ComponentType.METHOD))
    catalog.add_component(_component("pingCount", ComponentType.VARIABLE))

    codekb_service = object.__new__(CodeKBService)
    monkeypatch.setattr(
        CodeKBService,
        "_load_context",
        lambda self, space_id: SimpleNamespace(catalog=catalog),
    )
    return codekb_service


class TestSearchComponentsTypeFilter:
    def test_no_type_returns_every_match(self, codekb_service):
        names = {c["id"] for c in codekb_service.search_components("space", "*")}
        assert names == {"Ping", "Pong", "PingHandler", "pingCount"}

    def test_type_narrows_pattern_results(self, codekb_service):
        results = codekb_service.search_components("space", "*", component_type="class")
        assert {c["id"] for c in results} == {"Ping", "Pong"}

    def test_variable_type_excludes_classes(self, codekb_service):
        results = codekb_service.search_components("space", "*", component_type="variable")
        assert {c["id"] for c in results} == {"pingCount"}

    def test_type_is_case_insensitive(self, codekb_service):
        results = codekb_service.search_components("space", "*", component_type="CLASS")
        assert {c["id"] for c in results} == {"Ping", "Pong"}

    def test_pattern_and_type_both_apply(self, codekb_service):
        # Pattern alone matches three components (matching is case-insensitive,
        # so `pingCount` is included); the type narrows it to one.
        assert {c["id"] for c in codekb_service.search_components("space", "Ping*")} == {
            "Ping",
            "PingHandler",
            "pingCount",
        }
        results = codekb_service.search_components("space", "Ping*", component_type="method")
        assert {c["id"] for c in results} == {"PingHandler"}

    def test_unknown_type_returns_nothing(self, codekb_service):
        assert codekb_service.search_components("space", "*", component_type="widget") == []

    def test_type_with_no_matches_returns_empty(self, codekb_service):
        assert codekb_service.search_components("space", "*", component_type="enum") == []
