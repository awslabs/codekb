"""Per-component source content hashing for method-level delta refresh.

The space-refresh merge uses a stable per-component hash to detect which
components inside a *changed file* actually changed, so unchanged components
(e.g. sibling methods you didn't touch) are preserved instead of being
re-summarized. The hash covers only the component's own source lines, so it is
position-independent: a method that merely shifts down because lines were added
above hashes identically.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from ..core.logging_utils import get_logger

if TYPE_CHECKING:
    from ..core.models import Component

logger = get_logger(__name__)


def _read_lines(path: Path) -> Optional[List[str]]:
    """Return the file's lines, or None if it can't be read."""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # pragma: no cover - defensive; hashing must never break a refresh
        logger.debug("content_hash: could not read %s", path, exc_info=True)
        return None


def _resolve(base_dir: Path, component: "Component") -> Path:
    """Resolve a component's file path against base_dir when it's relative."""
    fp = component.file_path
    return fp if fp.is_absolute() else (base_dir / fp)


def _hash_lines(lines: List[str], component: "Component") -> str:
    """Hash the component's own source range out of a file's lines."""
    start = max(component.line_number - 1, 0)
    snippet = "\n".join(lines[start : component.end_line_number])
    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()


def compute_component_hash(base_dir: Path, component: "Component") -> Optional[str]:
    """Return a stable SHA-256 of a single component's source, or None if unreadable."""
    lines = _read_lines(_resolve(base_dir, component))
    if lines is None:
        return None
    return _hash_lines(lines, component)


def backfill_component_hashes(
    base_dir: Path,
    components: Iterable["Component"],
    *,
    overwrite: bool = True,
) -> int:
    """Set ``content_hash`` on components from their source under ``base_dir``.

    Reads each source file at most once. Returns the number of components whose
    hash was (re)computed. With ``overwrite=False`` only components that don't
    already have a hash are touched — used during refresh to fill in legacy
    (pre-feature) components without recomputing freshly-parsed ones.
    """
    cache: Dict[Path, Optional[List[str]]] = {}
    count = 0
    for component in components:
        if not overwrite and getattr(component, "content_hash", None):
            continue
        path = _resolve(base_dir, component)
        if path not in cache:
            cache[path] = _read_lines(path)
        lines = cache[path]
        if lines is None:
            continue
        component.content_hash = _hash_lines(lines, component)
        count += 1
    return count
