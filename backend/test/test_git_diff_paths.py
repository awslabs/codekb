"""diff_name_status returns non-ASCII file names as they are on disk (M11)."""

import subprocess
from pathlib import Path

import pytest

from src.analysis.git_ops import diff_name_status
from src.core.models import ChangeType


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, body in files.items():
        path = repo / name
        if body is None:
            path.unlink()
        else:
            path.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_non_ascii_names_come_back_as_on_disk(repo):
    names = ["追加.py", "café.py", "plain.py"]
    old = _commit(repo, {n: "def f():\n    pass\n" for n in names}, "one")
    new = _commit(repo, {n: "def f():\n    return 1\n" for n in names}, "two")

    changes = diff_name_status(repo, old, new)

    assert sorted(c.path for c in changes) == sorted(names)
    assert all(c.change_type == ChangeType.MODIFIED for c in changes)
    assert all((repo / c.path).exists() for c in changes)


def test_added_removed_and_renamed_non_ascii_files(repo):
    body = "def f():\n    return 'a long enough body for rename detection'\n"
    old = _commit(repo, {"old 名前.py": body, "削除.py": "x = 1\n"}, "one")
    (repo / "old 名前.py").rename(repo / "new 名前.py")
    new = _commit(repo, {"削除.py": None, "新規.py": "y = 2\n"}, "two")

    changes = {(c.path, c.change_type) for c in diff_name_status(repo, old, new)}

    assert changes == {
        ("old 名前.py", ChangeType.REMOVED),
        ("new 名前.py", ChangeType.ADDED),
        ("削除.py", ChangeType.REMOVED),
        ("新規.py", ChangeType.ADDED),
    }


def test_names_git_would_quote_come_back_as_on_disk(repo):
    body = "def f():\n    return 'a long enough body for rename detection'\n"
    old = _commit(repo, {"back\\slash.py": "x = 1\n", 'a "quoted" name.py': body}, "one")
    (repo / 'a "quoted" name.py').rename(repo / "tab\tname.py")
    new = _commit(repo, {"back\\slash.py": "x = 2\n", "new\\one.py": "y = 1\n"}, "two")

    changes = {(c.path, c.change_type) for c in diff_name_status(repo, old, new)}

    assert changes == {
        ("back\\slash.py", ChangeType.MODIFIED),
        ("new\\one.py", ChangeType.ADDED),
        ('a "quoted" name.py', ChangeType.REMOVED),
        ("tab\tname.py", ChangeType.ADDED),
    }
