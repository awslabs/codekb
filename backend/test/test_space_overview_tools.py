"""Overview agent tools that walk a space's source tree."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.vector.space_overview_generator import SpaceOverviewGenerator


class _FakeService:
    def __init__(self, root: Path):
        self.root = root

    def ensure_source_local(self, space_id: str) -> Path:
        return self.root

    def source_subdir(self, space_id: str, rel_path: str = "") -> Path:
        return self.root / rel_path


@pytest.fixture
def list_tree(tmp_path):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "top.py").write_text("x")
    (tmp_path / "pkg" / "a.py").write_text("x")
    (tmp_path / "pkg" / "sub" / "b.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.js").write_text("x")

    with patch(
        "src.service.codekb_service.CodeKBService.instance",
        return_value=_FakeService(tmp_path),
    ):
        tools = SpaceOverviewGenerator("space")._get_tools()
    return next(t for t in tools if getattr(t, "tool_name", "") == "list_source_files_recursive")


def test_lists_the_whole_tree_from_the_root(list_tree):
    assert list_tree(directory="").split("\n") == [
        "./",
        "  top.py",
        "pkg/",
        "  a.py",
        "  sub/",
        "    b.py",
    ]


def test_subdirectory_keeps_its_depth_from_the_root(list_tree):
    assert list_tree(directory="pkg").split("\n") == [
        "pkg/",
        "  a.py",
        "  sub/",
        "    b.py",
    ]
