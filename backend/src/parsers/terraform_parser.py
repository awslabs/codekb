"""
Terraform (HCL) parser implementation.

Terraform is declarative, not imperative, so this parser reinterprets the
project's "call graph" abstraction as Terraform's *dependency graph*:

  - A top-level HCL block (resource, data, module, output, provider) and each
    entry in a `locals` block become a Component (the "node").
  - A reference inside a block body (e.g. ``var.region``,
    ``aws_s3_bucket.logs.arn``, ``module.vpc.subnet_ids``) becomes an edge from
    the enclosing block to the referenced block (the "call").

Component IDs are Terraform addresses (``aws_s3_bucket.logs``, ``var.region``,
``module.vpc``, ``data.aws_ami.ubuntu``) rather than file-stem-prefixed names,
because references address blocks globally within a module and never mention
the defining file.

Type mapping note: the flow analyzer only scans FUNCTION/METHOD components for
outgoing edges, while any component type is a valid *target*. So every block
that can reference other blocks is typed FUNCTION, and `variable` blocks (pure
inputs that never reference other blocks) are typed VARIABLE. This is the
deliberate "fit the existing abstraction" trade-off; a dedicated RESOURCE
component type would be a later, more faithful refinement.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import (
    Component,
    ComponentType,
    ComponentAttributes,
    Parameter,
    Visibility,
    IdScheme,
)


# Reference-address truncation: how many leading segments identify the block a
# reference points at. ``var.region`` -> var.region (2), ``data.aws_ami.x`` ->
# data.aws_ami.x (3). Everything else (managed resources) is TYPE.NAME (2),
# e.g. ``aws_s3_bucket.logs.arn`` -> aws_s3_bucket.logs.
_REFERENCE_SCOPE_SEGMENTS = {"var": 2, "local": 2, "module": 2, "data": 3}
_DEFAULT_SCOPE_SEGMENTS = 2

# Block kinds we turn into components, and how their address is formed from the
# block's labels. The value is the address prefix; None means "use the labels
# directly" (managed resources: TYPE.NAME).
_ADDRESS_PREFIX = {
    "resource": None,  # aws_s3_bucket.logs
    "data": "data",  # data.aws_ami.ubuntu
    "module": "module",  # module.vpc
    "variable": "var",  # var.region
    "output": "output",  # output.instance_ip
    "provider": "provider",  # provider.aws
}


class TerraformParser(BaseLanguageParser):
    """Terraform/HCL parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """HCL reference "calls" are `expression` nodes led by a variable_expr."""
        return ["expression"]

    def get_function_node_types(self) -> List[str]:
        """No nested callables are modeled.

        Returning an empty list tells the flow analyzer not to treat nested
        HCL blocks as call boundaries, so references inside nested blocks
        (e.g. ``ingress { cidr_blocks = [var.cidr] }``) are attributed to the
        enclosing top-level block rather than dropped.
        """
        return []

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract a normalized reference address from an expression's head.

        ``function_node`` is ``expression.children[0]``. For a reference this is
        a ``variable_expr`` followed by ``get_attr`` siblings. We collect the
        dotted path (stopping at any index like ``[0]``) and truncate it to the
        referenced block's address using ``_REFERENCE_SCOPE_SEGMENTS``.
        """
        if function_node is None or function_node.type != "variable_expr":
            return None

        parts = [self._get_node_text_simple(function_node)]
        sibling = function_node.next_named_sibling
        while sibling is not None:
            if sibling.type != "get_attr":
                # An index (`[0]`), operator, or call ends the reference path.
                break
            attr_name = None
            for child in sibling.children:
                if child.type == "identifier":
                    attr_name = self._get_node_text_simple(child)
            if attr_name:
                parts.append(attr_name)
            sibling = sibling.next_named_sibling

        if not parts or not parts[0]:
            return None

        segments = _REFERENCE_SCOPE_SEGMENTS.get(parts[0], _DEFAULT_SCOPE_SEGMENTS)
        if len(parts) < segments:
            # Incomplete reference (e.g. a bare `var`) — nothing to resolve.
            return None
        return ".".join(parts[:segments])

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from a Terraform syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        # Top-level blocks live directly under the file's `body` node.
        for node in self._iter_blocks(root_node):
            kind = self._block_kind(node)
            if kind == "locals":
                components.extend(self._extract_locals_components(node, source_lines, file_path))
            elif kind in _ADDRESS_PREFIX:
                component = self._extract_block_component(node, kind, source_lines, file_path)
                if component:
                    components.append(component)
            # Other kinds (e.g. `terraform` settings) are intentionally skipped.

        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """HCL blocks have no parameter lists."""
        return []

    def _iter_blocks(self, root_node: Any):
        """Yield the top-level `block` nodes of a config file."""
        for child in root_node.children:
            if child.type == "body":
                for grandchild in child.children:
                    if grandchild.type == "block":
                        yield grandchild
            elif child.type == "block":
                # Defensive: some grammars may surface blocks directly.
                yield child

    def _block_kind(self, block_node: Any) -> Optional[str]:
        """Return the block's leading identifier (resource, variable, ...)."""
        for child in block_node.children:
            if child.type == "identifier":
                return self._get_node_text_simple(child)
        return None

    def _block_labels(self, block_node: Any) -> List[str]:
        """Return the quoted labels of a block (e.g. ["aws_s3_bucket", "logs"])."""
        labels: List[str] = []
        for child in block_node.children:
            if child.type == "string_lit":
                labels.append(self._get_node_text_simple(child).strip('"'))
        return labels

    def _extract_block_component(
        self, node: Any, kind: str, source_lines: List[str], file_path: Path
    ) -> Optional[Component]:
        """Build a component for a labeled top-level block."""
        try:
            labels = self._block_labels(node)
            address = self._build_address(kind, labels)
            if not address:
                return None

            # `variable` blocks are pure inputs (valid targets, never callers),
            # so they are the one kind that is not scanned for outgoing edges.
            component_type = (
                ComponentType.VARIABLE if kind == "variable" else ComponentType.FUNCTION
            )

            signature = self._block_signature(kind, labels)
            attributes = ComponentAttributes(
                signature=signature,
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._terraform_component(
                address, component_type, node, file_path, attributes
            )
        except Exception:
            return None

    def _extract_locals_components(
        self, block_node: Any, source_lines: List[str], file_path: Path
    ) -> List[Component]:
        """Extract one component per entry in a `locals` block.

        A ``locals { a = ...  b = ... }`` block defines the addresses
        ``local.a`` and ``local.b``, each of which can reference other blocks,
        so each entry becomes its own FUNCTION component scoped to the
        attribute's line range.
        """
        components: List[Component] = []
        try:
            body = next((c for c in block_node.children if c.type == "body"), None)
            if body is None:
                return components

            for attribute in body.children:
                if attribute.type != "attribute":
                    continue
                name_node = next((c for c in attribute.children if c.type == "identifier"), None)
                if name_node is None:
                    continue
                local_name = self._get_node_text_simple(name_node)
                address = f"local.{local_name}"

                attributes = ComponentAttributes(
                    signature=f"local.{local_name}",
                    visibility=Visibility.PUBLIC,
                    line_count=attribute.end_point[0] - attribute.start_point[0] + 1,
                )

                components.append(
                    self._terraform_component(
                        address, ComponentType.FUNCTION, attribute, file_path, attributes
                    )
                )
        except Exception:
            pass
        return components

    def _terraform_component(
        self,
        address: str,
        component_type: ComponentType,
        node: Any,
        file_path: Path,
        attributes: ComponentAttributes,
    ) -> Component:
        """Build a Terraform component with a file-scoped, collision-free id.

        A Terraform address (`aws_iam_role.ecs_task_role`) is unique only WITHIN
        a module, so using it as the id verbatim makes the same address in
        different stacks overwrite each other (whole stacks were lost this way).
        Prefix the id with the file's scheme prefix (the V2 path hash) so stacks
        stay distinct. The address stays at the end of the id, which is where
        `Component.get_simple_name` reads it back as the key references resolve
        against.

        Only V2 spaces are prefixed. Legacy V1 spaces keep address-only ids
        exactly as before, and adopt the prefixed form only on a deliberate
        full V2 reindex — the same per-space scheme rule every other language
        follows.
        """
        if self._scheme != IdScheme.V2_PATH:
            return self._create_component(
                address, component_type, node, file_path, "terraform", attributes
            )

        return self._create_component(
            self.make_id(file_path, address),
            component_type,
            node,
            file_path,
            "terraform",
            attributes,
        )

    @staticmethod
    def _build_address(kind: str, labels: List[str]) -> Optional[str]:
        """Compose a Terraform address from a block kind and its labels."""
        prefix = _ADDRESS_PREFIX.get(kind)
        if prefix is None:
            # Managed resource: TYPE.NAME needs both labels.
            if len(labels) < 2:
                return None
            return f"{labels[0]}.{labels[1]}"
        if kind == "data":
            # data.TYPE.NAME needs both labels.
            if len(labels) < 2:
                return None
            return f"data.{labels[0]}.{labels[1]}"
        # var/module/output/provider: single label.
        if not labels:
            return None
        return f"{prefix}.{labels[0]}"

    @staticmethod
    def _block_signature(kind: str, labels: List[str]) -> str:
        """Human-readable block header, e.g. resource "aws_s3_bucket" "logs"."""
        quoted = " ".join(f'"{label}"' for label in labels)
        return f"{kind} {quoted}".strip()
