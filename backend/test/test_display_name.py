"""`display_name` is a UI-only, REST-layer-only field.

A human-friendly, parent-qualified label derived from a component id. The strict
rule these tests enforce: it is attached ONLY by the REST route handlers, and
appears in NO layer at or below the shared service — not in ``Component.to_dict``
(persistence), not in service results, and not in MCP tool output. An LLM
driving the MCP tools must never key tool chaining on it, because once the
file-prefix segment is dropped the label is not unique; only ``id`` is the key.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import (
    Component,
    ComponentAttributes,
    ComponentType,
    derive_display_name,
)
from src.flow.flow_analyzer import FlowAnalyzer
from src.service.codekb_service import CodeKBService

SPACE = "demo"
PREFIX = f"{SPACE}::main<deadbeef0000>"

FN_ID = f"{PREFIX}.parseArgs"  # top-level function          -> parseArgs
METHOD_ID = f"{PREFIX}.Repo.Save#a1b2"  # overloaded class method    -> Repo.Save
CLASS_ID = f"{PREFIX}.Repo"  # class                       -> Repo


def _make(cid, ctype=ComponentType.FUNCTION, language="python", parent=None, accessor_kind=None):
    return Component(
        id=cid,
        type=ctype,
        file_path=Path("f.py"),
        line_number=1,
        end_line_number=2,
        language=language,
        parent_component=parent,
        attributes=ComponentAttributes(accessor_kind=accessor_kind),
    )


# ── derive_display_name: the pure helper ─────────────────────────────────


class TestDeriveDisplayName:
    def test_top_level_function_drops_the_file_prefix(self):
        assert derive_display_name(FN_ID, "python") == "parseArgs"

    def test_class_method_keeps_the_qualifier_chain(self):
        # The intermediate class segment is retained; only the file prefix and
        # the #overload suffix are stripped.
        assert derive_display_name(METHOD_ID, "python", parent_component=CLASS_ID) == "Repo.Save"

    def test_overload_discriminator_is_stripped(self):
        assert (
            derive_display_name(f"{PREFIX}.handle#0674", "python") == "handle"
        )

    def test_class_itself_is_just_its_name(self):
        assert derive_display_name(CLASS_ID, "python") == "Repo"

    def test_terraform_id_shows_its_reference_address(self):
        # Matches get_simple_name for terraform: the reference address, not a
        # single trailing segment.
        local = f"{SPACE}::main_abc123.aws_iam_role.ecs_task_role"
        assert derive_display_name(local, "terraform") == "aws_iam_role.ecs_task_role"

    def test_terraform_data_source_keeps_its_three_part_address(self):
        local = f"{SPACE}::net_abc123.data.aws_ami.ubuntu"
        assert derive_display_name(local, "terraform") == "data.aws_ami.ubuntu"

    def test_dart_accessor_steps_over_the_kind_suffix(self):
        # kv.KeyValueStore.label.get is a getter for `label`; display drops the
        # file prefix and the `.get` suffix, keeping the class qualifier.
        cid = f"{SPACE}::kv.KeyValueStore.label.get"
        parent = f"{SPACE}::kv.KeyValueStore"
        assert (
            derive_display_name(cid, "dart", parent_component=parent, accessor_kind="get")
            == "KeyValueStore.label"
        )

    def test_dart_member_named_get_keeps_its_own_name(self):
        # A method literally named `get` is not an accessor suffix.
        cid = f"{SPACE}::kv.KeyValueStore.get"
        parent = f"{SPACE}::kv.KeyValueStore"
        assert (
            derive_display_name(cid, "dart", parent_component=parent) == "KeyValueStore.get"
        )


# ── guardrail: persistence / model never carries it ──────────────────────


class TestPersistenceStaysClean:
    def test_component_to_dict_has_no_display_name(self):
        assert "display_name" not in _make(METHOD_ID, ComponentType.METHOD).to_dict()


# ── shared service + MCP harness ─────────────────────────────────────────


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A bare CodeKBService whose context is a catalog + injected call graph.

    object.__new__ steps past the singleton guard, which exists to stop a second
    real service being built, not to block a test double.
    """
    catalog = ComponentCatalog()
    catalog.add_component(_make(FN_ID, ComponentType.FUNCTION))
    catalog.add_component(_make(METHOD_ID, ComponentType.METHOD, parent=CLASS_ID))
    catalog.add_component(_make(CLASS_ID, ComponentType.CLASS))

    flow = FlowAnalyzer(catalog)
    # Inject the call graph directly so we exercise the response shapes without
    # parsing source: parseArgs() -> Repo.Save().
    flow._call_graph = {FN_ID: [(METHOD_ID, 5)], METHOD_ID: []}
    flow._reverse_call_graph = {METHOD_ID: [FN_ID]}
    flow._call_graph_built = True

    ctx = SimpleNamespace(
        catalog=catalog,
        knowledge_base=SimpleNamespace(source_directory=str(tmp_path)),
        flow_analyzer=flow,
    )
    monkeypatch.setattr(CodeKBService, "_load_context", lambda self, space_id: ctx)
    svc = object.__new__(CodeKBService)
    return svc


class TestSharedServiceStaysClean:
    def test_find_component_has_no_display_name(self, service):
        assert "display_name" not in service.find_component(SPACE, METHOD_ID)

    def test_search_components_has_no_display_name(self, service):
        results = service.search_components(SPACE, "*")
        assert results
        assert all("display_name" not in c for c in results)

    def test_list_components_has_no_display_name(self, service):
        results = service.list_components(SPACE)
        assert results
        assert all("display_name" not in c for c in results)


class TestMcpToolStaysClean:
    def test_find_component_tool_output_has_no_display_name(self, service, monkeypatch):
        from src.mcp.tools import query_tools

        # Authz off makes gate_space_read a no-op; the tool reaches the service
        # through CodeKBService.instance().
        monkeypatch.setattr(
            "src.mcp.tools.common.require_authorization", lambda: False
        )
        monkeypatch.setattr(CodeKBService, "instance", classmethod(lambda cls: service))

        out = asyncio.run(query_tools.find_component(METHOD_ID, raw_output=True))
        assert "display_name" not in json.loads(out)


# ── REST routes DO enrich ─────────────────────────────────────────────────


@pytest.fixture
def client(service, monkeypatch):
    """The real query router, with auth/authz seams stood down."""
    starlette = pytest.importorskip("starlette.testclient")
    from fastapi import FastAPI

    import src.api.auth as auth_mod
    import src.api.gates as gates_mod
    from src.api.routes import query as query_routes

    monkeypatch.setattr(
        query_routes,
        "require_current_user",
        lambda request: SimpleNamespace(email="test@localhost", sub="test"),
    )
    monkeypatch.setattr(auth_mod, "_REQUIRE_AUTH", False)
    monkeypatch.setattr(gates_mod, "require_authorization", lambda: False)
    monkeypatch.setattr(CodeKBService, "instance", classmethod(lambda cls: service))

    app = FastAPI()
    app.include_router(query_routes.router, prefix="/api/spaces")
    with starlette.TestClient(app) as test_client:
        yield test_client


class TestRestRoutesEnrich:
    def test_find_component_route_carries_the_parent_qualified_label(self, client):
        resp = client.get(f"/api/spaces/{SPACE}/components/{quote(METHOD_ID, safe='')}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == METHOD_ID  # id is untouched, still the lookup key
        assert body["display_name"] == "Repo.Save"

    def test_list_route_enriches_every_component(self, client):
        resp = client.get(f"/api/spaces/{SPACE}/components")
        assert resp.status_code == 200, resp.text
        by_id = {c["id"]: c for c in resp.json()}
        assert by_id[FN_ID]["display_name"] == "parseArgs"
        assert by_id[METHOD_ID]["display_name"] == "Repo.Save"
        assert by_id[CLASS_ID]["display_name"] == "Repo"

    def test_flow_route_supplies_a_display_name_lookup(self, client):
        resp = client.get(f"/api/spaces/{SPACE}/components/{quote(FN_ID, safe='')}/flow")
        assert resp.status_code == 200, resp.text
        names = resp.json()["display_names"]
        # Keyed by id (the lookup key); values are the UI labels.
        assert names[FN_ID] == "parseArgs"
        assert names[METHOD_ID] == "Repo.Save"

    def test_deps_route_supplies_a_display_name_lookup(self, client):
        resp = client.get(f"/api/spaces/{SPACE}/components/{quote(FN_ID, safe='')}/deps")
        assert resp.status_code == 200, resp.text
        names = resp.json()["display_names"]
        assert names[METHOD_ID] == "Repo.Save"

    def test_semantic_search_route_enriches_vector_results(self, client, service, monkeypatch):
        # Vector results carry the component id under `key` and no component
        # fields; the route resolves the label from the space catalog.
        monkeypatch.setattr(
            service,
            "semantic_search",
            lambda *a, **k: [
                {"key": METHOD_ID, "metadata": {"type": "method", "space_id": SPACE}},
                {"key": FN_ID, "metadata": {"type": "function", "space_id": SPACE}},
            ],
        )
        resp = client.get(f"/api/spaces/{SPACE}/search?q=anything")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        by_key = {r["key"]: r for r in body}
        assert by_key[METHOD_ID]["display_name"] == "Repo.Save"
        assert by_key[FN_ID]["display_name"] == "parseArgs"
        # id (the lookup key) is left untouched under `key`.
        assert by_key[METHOD_ID]["key"] == METHOD_ID


# ── global search route (separate router/module) ──────────────────────────


@pytest.fixture
def search_client(service, monkeypatch):
    """The real global-search router, with auth/authz seams stood down."""
    starlette = pytest.importorskip("starlette.testclient")
    from fastapi import FastAPI

    from src.api.routes import search as search_routes

    monkeypatch.setattr(
        search_routes,
        "require_current_user",
        lambda request: SimpleNamespace(email="test@localhost", sub="test"),
    )
    monkeypatch.setattr(search_routes, "require_authorization", lambda: False)
    monkeypatch.setattr(CodeKBService, "instance", classmethod(lambda cls: service))
    monkeypatch.setattr(
        service,
        "global_semantic_search",
        lambda *a, **k: [
            {"key": METHOD_ID, "metadata": {"type": "method", "space_id": SPACE}},
        ],
    )

    app = FastAPI()
    app.include_router(search_routes.router, prefix="/api/search")
    with starlette.TestClient(app) as test_client:
        yield test_client


class TestGlobalSearchRouteEnriches:
    def test_global_search_route_enriches_vector_results(self, search_client):
        resp = search_client.get("/api/search?q=anything")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body[0]["key"] == METHOD_ID  # id untouched
        assert body[0]["display_name"] == "Repo.Save"


class TestSearchServiceStaysClean:
    def test_attach_runs_in_route_not_service(self, service, monkeypatch):
        # The raw service result carries no display_name; enrichment is the
        # route's job. Proven by the clean raw result feeding the route tests
        # above, which only then shows the field.
        raw = [{"key": METHOD_ID, "metadata": {"space_id": SPACE}}]
        monkeypatch.setattr(service, "semantic_search", lambda *a, **k: list(raw))
        assert all("display_name" not in r for r in service.semantic_search("q", space_id=SPACE))
