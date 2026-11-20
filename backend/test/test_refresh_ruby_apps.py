"""Refresh gives a re-parsed Ruby file the same component IDs creation does (M5)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import ChangeType, FileChange, IdScheme

tree_sitter_ruby = pytest.importorskip("tree_sitter_ruby")

GEMFILE = "source 'https://rubygems.org'\n"
USER_RB = "class User\n  def save\n    1\n  end\nend\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def _creation_ids(root: Path) -> set[str]:
    kb = CodeAnalyzer(ComponentCatalog()).analyze_directory(root, space_id="space", id_scheme=IdScheme.V1_STEM)
    return {c.id for c in kb.components}


def _refresh_ids(root: Path, changed: list[str]) -> set[str]:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch._clone_dir = root
    orch._file_changes = [FileChange(path, ChangeType.MODIFIED) for path in changed]
    orch._step_parse_changed()
    return {c.id for c in orch._parsed_components}


def test_nested_app_file_keeps_its_app_prefix(tmp_path):
    _write(
        tmp_path,
        {
            "Gemfile": GEMFILE,
            "app/models/user.rb": USER_RB,
            "services/billing/Gemfile": GEMFILE,
            "services/billing/app/models/user.rb": USER_RB,
        },
    )

    ids = _refresh_ids(tmp_path, ["services/billing/app/models/user.rb"])

    assert ids == {"space::services.billing.user.User", "space::services.billing.user.User.save"}
    assert ids <= _creation_ids(tmp_path)


def test_root_app_and_single_app_repos_keep_unprefixed_ids(tmp_path):
    _write(tmp_path, {"Gemfile": GEMFILE, "app/models/user.rb": USER_RB})

    ids = _refresh_ids(tmp_path, ["app/models/user.rb"])

    assert ids == {"space::user.User", "space::user.User.save"}
    assert ids <= _creation_ids(tmp_path)


def test_a_refresh_without_ruby_changes_skips_the_app_root_scan(tmp_path, monkeypatch):
    _write(tmp_path, {"Gemfile": GEMFILE, "app/models/user.rb": USER_RB, "lib/util.py": "def f():\n    pass\n"})
    built = MagicMock()
    monkeypatch.setattr("src.analysis.code_analyzer.RubyAppBoundaryQualifier", built)

    _refresh_ids(tmp_path, ["lib/util.py"])

    built.assert_not_called()
