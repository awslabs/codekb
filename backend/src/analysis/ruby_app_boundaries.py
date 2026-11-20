"""Application-boundary qualification for Ruby component IDs.

Why this exists
---------------
Ruby's constant namespace is flat per *process*: ``class User`` in two files of
one application refers to the same runtime constant — the second file reopens
the class. CodeKB's catalog merging same-ID components therefore mirrors Ruby
semantics for a single application, and directory-based ID qualification (the
remedy appropriate for Python/TypeScript/JavaScript/Rust) would be wrong for
Ruby: it would split one runtime class into two artificial components and break
call edges that resolve correctly today.

What is genuinely ambiguous is the same top-level constant defined in two
*different applications* sharing one repository — separate Gemfiles, separate
processes, genuinely distinct classes. The correct disambiguator is therefore
the application boundary, not the directory: the nearest ancestor directory
containing a ``Gemfile`` or ``*.gemspec``.

Behavior
--------
- Zero or one application root in the repo (the common case, including every
  standard Rails app): **no change to any ID**. This keeps IDs stable and
  avoids inventing a second ID convention for the majority of repositories.
- Multiple application roots: components of files under a *nested* root are
  prefixed with that root's repo-relative path, dotted
  (``services/billing`` -> ``services.billing.user.User``). Files under the
  repository root's own application (or under no root at all) keep their
  unprefixed IDs, so the primary app's IDs stay stable when a sub-app appears.

The prefix is applied to ``Component.id`` and ``parent_component`` before the
component enters the catalog — the collision is a last-write-wins overwrite at
insert time, so post-hoc rewriting could not recover the lost component.

Caveat recorded on P492844907: qualification is computed from the repository
state at analysis time, so adding or removing a Gemfile between analyses can
change IDs for the affected sub-app, with the usual persisted-ID implications
for space refresh.
"""

from pathlib import Path
from typing import Callable, List, Optional

from ..core.logging_utils import get_logger
from ..core.models import Component

logger = get_logger(__name__)

# Files that mark a directory as a Ruby application/gem root.
_APP_ROOT_MARKERS = ("Gemfile",)
_APP_ROOT_SUFFIXES = (".gemspec",)


class RubyAppBoundaryQualifier:
    """Prefixes Ruby component IDs with their application root, when ambiguous.

    Instantiate once per analysis run with the source directory; apply to each
    file's components before they are added to the catalog.
    """

    def __init__(
        self,
        source_directory: Path,
        is_excluded_directory: Optional[Callable[[Path], bool]] = None,
    ) -> None:
        """Scan ``source_directory`` for Ruby application roots.

        Args:
            source_directory: Repository root being analyzed.
            is_excluded_directory: Optional predicate (typically
                ``FileScanner._is_excluded_directory``) so the marker walk
                skips the same directories the analysis skips.
        """
        self._source_directory = source_directory.resolve()
        self._app_roots = self._find_app_roots(is_excluded_directory)
        # Deepest-first so nearest-ancestor matching is a simple first-hit scan.
        self._nested_roots = sorted(
            (root for root in self._app_roots if root != self._source_directory),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        if self.active:
            logger.info(
                "Multiple Ruby application roots detected; qualifying nested-app "
                f"component IDs: {[str(r.relative_to(self._source_directory)) for r in self._nested_roots]}"
            )

    @property
    def active(self) -> bool:
        """Whether qualification will change any ID.

        True only when the repo contains more than one application root AND at
        least one of them is nested (a lone nested root with no repo-root app
        still means exactly one application — nothing is ambiguous).
        """
        return len(self._app_roots) > 1 and bool(self._nested_roots)

    def qualify(self, components: List[Component]) -> None:
        """Prefix Ruby components belonging to a nested application, in place."""
        if not self.active:
            return
        for component in components:
            if component.language != "ruby":
                continue
            prefix = self._prefix_for(component.file_path)
            if not prefix:
                continue
            component.id = f"{prefix}.{component.id}"
            if component.parent_component:
                component.parent_component = f"{prefix}.{component.parent_component}"

    # ── internals ────────────────────────────────────────────────────────────

    def _find_app_roots(
        self, is_excluded_directory: Optional[Callable[[Path], bool]]
    ) -> List[Path]:
        """Directories containing a Gemfile or *.gemspec, excludes respected."""
        roots: set = set()
        stack = [self._source_directory]
        while stack:
            directory = stack.pop()
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if (
                        entry.is_symlink()
                        or entry.name.startswith(".")
                        or (is_excluded_directory is not None and is_excluded_directory(entry))
                    ):
                        continue
                    stack.append(entry)
                elif entry.name in _APP_ROOT_MARKERS or entry.suffix in _APP_ROOT_SUFFIXES:
                    roots.add(directory)
        return sorted(roots)

    def _prefix_for(self, file_path: Path) -> Optional[str]:
        """Dotted repo-relative path of the file's nearest nested app root."""
        try:
            resolved = file_path.resolve()
        except OSError:
            return None
        for root in self._nested_roots:
            if resolved == root or root in resolved.parents:
                return ".".join(root.relative_to(self._source_directory).parts)
        return None
