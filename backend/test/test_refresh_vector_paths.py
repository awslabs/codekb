"""Vectors a refresh writes carry the source-cache path creation stores, not the deleted clone's."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.models import Component, ComponentType
from src.service.codekb_service import WORK_DIR_REFRESH, CodeKBService


def _function(file_path: Path) -> Component:
    return Component(
        id="sp::x.f",
        type=ComponentType.FUNCTION,
        file_path=file_path,
        line_number=1,
        end_line_number=2,
        language="python",
        space_id="sp",
    )


def test_a_refresh_clone_path_becomes_the_source_cache_path():
    svc = CodeKBService.instance()
    clone = svc.source_provider.work_path("sp", WORK_DIR_REFRESH)

    stored = svc._vector_file_path("sp", clone / "src/x.py")

    assert stored == str(svc.source_provider.cache_path("sp") / "src/x.py")


def test_creation_and_relative_paths_are_unchanged():
    svc = CodeKBService.instance()
    cache_file = svc.source_provider.cache_path("sp") / "src/x.py"

    assert svc._vector_file_path("sp", cache_file) == str(cache_file)
    assert svc._vector_file_path("sp", Path("src/x.py")) == "src/x.py"


def test_indexing_during_a_refresh_stores_the_source_cache_path(monkeypatch):
    svc = CodeKBService.instance()
    clone = svc.source_provider.work_path("sp", WORK_DIR_REFRESH)
    comp = _function(clone / "src/x.py")
    monkeypatch.setattr(
        svc, "_load_context", lambda sid: SimpleNamespace(catalog=SimpleNamespace(find_component=lambda key: comp))
    )

    with patch("src.vector.s3_vectors_client.S3VectorsClient") as client:
        svc.index_component_description("sp", "sp::x.f", "Does f.")

    metadata = client.return_value.insert_document.call_args.kwargs["metadata"]
    assert metadata["file_path"] == str(svc.source_provider.cache_path("sp") / "src/x.py")
