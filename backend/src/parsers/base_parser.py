"""
Base parser interface for language-specific parsing logic.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Any
from pathlib import Path

from ..core.models import (
    Component,
    ComponentType,
    ComponentAttributes,
    Parameter,
    ParseDiagnostic,
    IdScheme,
    DEFAULT_SCHEME,
    NAME_SEP,
    path_prefix,
)


class BaseLanguageParser(ABC):
    """Abstract base class for language-specific parsers."""

    # Per-run id context, injected by the analyzer/wrapper before parsing (see
    # TreeSitterWrapper.parse_file). `_scheme` selects the local-id format and
    # `_root` is the repo root used to derive V2 path-hashed prefixes. Every
    # parser builds ids via `make_id` so the scheme lives in exactly one place.
    _scheme: IdScheme = DEFAULT_SCHEME
    _root: Optional[Path] = None

    def make_id(
        self,
        file_path: Path,
        name: str,
        parent_local_id: Optional[str] = None,
        *,
        top_level_id: Optional[str] = None,
    ) -> str:
        """Build a local component id (without the `space_id::` prefix).

        Args:
            file_path: Source file the component is defined in.
            name: The component's bare (simple) name.
            parent_local_id: Local id of the enclosing component or namespace.
                When set, the id is chained onto it (`parent.name`) and the
                file prefix is NOT applied — this preserves the namespace/nesting
                behavior of the C#, PHP, and C++ parsers.
            top_level_id: A fully-formed local id supplied by the parser, used
                verbatim. For parsers that own their addressing scheme (e.g.
                Terraform addresses) and must never be file-prefixed.

        Returns:
            The local component id.
        """
        if top_level_id is not None:
            return top_level_id
        if parent_local_id:
            return f"{parent_local_id}{NAME_SEP}{name}"
        return f"{self.file_prefix(file_path)}{NAME_SEP}{name}"

    def file_prefix(self, file_path: Path) -> str:
        """Return the scheme-dependent top-level prefix for a file.

        V1_STEM → the file stem; V2_PATH → a stable hash of the repo-relative
        path. Exposed for parsers that pass the top-level prefix down as a parent
        (e.g. JavaScript object-method extraction) rather than calling make_id.
        """
        if self._scheme == IdScheme.V2_PATH:
            return path_prefix(file_path, self._root)
        return file_path.stem

    @abstractmethod
    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls."""
        pass

    @abstractmethod
    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions."""
        pass

    @abstractmethod
    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from call node."""
        pass

    @abstractmethod
    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from AST."""
        pass

    @abstractmethod
    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from function node."""
        pass

    def drain_parse_diagnostics(self) -> List["ParseDiagnostic"]:
        """Return and clear the diagnostics the last extraction recorded.
        The grammar's own ERROR and MISSING nodes are collected by the wrapper's
        tree walk, which needs no help from a parser.
        """
        return []

    def _get_node_text(self, node: Any, source_lines: Optional[List[str]] = None) -> str:
        """Get the exact source text of a syntax tree node.

        Reads the node's own bytes (``node.text``) rather than slicing the
        decoded ``source_lines`` by the node's column offsets. Tree-sitter
        reports positions as BYTE offsets, but ``source_lines`` holds decoded
        CHARACTERS — so slicing by those offsets corrupts any name preceded on
        its line by a multi-byte sequence. A UTF-8 BOM (3 bytes, 1 character,
        written by Visual Studio and others at the start of a file) chops the
        first two characters off the first line's names; any earlier non-ASCII
        character shifts them the same way, in every language. Because the name
        feeds the component id, that corrupts ids and breaks lookup by name.

        ``node.text`` is the node's exact byte span, so decoding it is correct
        regardless of BOMs or multi-byte characters. For pure-ASCII content it
        is byte-identical to the old slice, so existing ids are unchanged; only
        previously-corrupted ids are repaired. ``source_lines`` is kept for
        signature compatibility and as a fallback when ``node.text`` is absent.
        """
        text = getattr(node, "text", None)
        if text is not None:
            try:
                return text.decode("utf-8", "replace")
            except Exception:
                pass

        if source_lines is None:
            return "unknown"
        try:
            start_row, start_col = node.start_point
            end_row, end_col = node.end_point

            if start_row == end_row:
                return source_lines[start_row][start_col:end_col]
            else:
                lines = []
                lines.append(source_lines[start_row][start_col:])
                for row in range(start_row + 1, end_row):
                    lines.append(source_lines[row])
                lines.append(source_lines[end_row][:end_col])
                return "\n".join(lines)
        except (IndexError, AttributeError):
            return "unknown"

    def _get_node_text_simple(self, node: Any) -> str:
        """Get simple text content of a node."""
        try:
            if hasattr(node, "text"):
                return node.text.decode("utf-8")
            return "unknown"
        except Exception:
            return "unknown"

    def _create_component(
        self,
        component_id: str,
        component_type: ComponentType,
        node: Any,
        file_path: Path,
        language: str,
        attributes: ComponentAttributes,
        parent_component: Optional[str] = None,
    ) -> Component:
        """Create component with common fields."""
        return Component(
            id=component_id,
            type=component_type,
            file_path=file_path,
            line_number=node.start_point[0] + 1,
            end_line_number=node.end_point[0] + 1,
            language=language,
            attributes=attributes,
            parent_component=parent_component,
        )
