"""
PHP-specific parser implementation.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


# Node types that introduce a new container (class/interface/trait/enum).
_CONTAINER_NODE_TYPES = [
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
]

# Bodies that hold container members.
_BODY_NODE_TYPES = ["declaration_list", "enum_declaration_list"]


class PhpParser(BaseLanguageParser):
    """PHP-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in PHP."""
        return [
            "function_call_expression",
            "member_call_expression",
            "scoped_call_expression",
            "object_creation_expression",
        ]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in PHP."""
        return ["function_definition", "method_declaration"]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """
        Extract a resolvable call name from a PHP call node.

        The flow analyzer passes ``call_node.children[0]`` here, so the node we
        receive depends on the call form:
          - ``function_call_expression`` -> ``name`` (e.g. ``trim``)
          - ``member_call_expression``   -> ``variable_name`` (e.g. ``$this``)
          - ``scoped_call_expression``   -> ``name``/``relative_scope`` (the class)
          - ``object_creation_expression`` -> the ``new`` keyword
        We use the parent to reconstruct names like ``self.method``,
        ``Class.method`` or ``var.method`` that the resolver understands.
        """
        parent = getattr(function_node, "parent", None)
        parent_type = parent.type if parent is not None else None

        # $obj->method(...) / $this->method(...)
        if parent_type == "member_call_expression":
            method = self._method_name_after_operator(parent)
            if not method:
                return None
            obj = self._variable_name(function_node)
            if obj in (None, "this"):
                return f"self.{method}"
            return f"{obj}.{method}"

        # Class::method(...) / self::method(...) / parent::method(...)
        if parent_type == "scoped_call_expression":
            method = self._method_name_after_operator(parent)
            if not method:
                return None
            if function_node.type == "relative_scope":
                scope = self._get_node_text_simple(function_node).lower()
                if scope in ("self", "static"):
                    return f"self.{method}"

                # parent:: names the base class of the enclosing class. Without a
                # base clause (a trait, a class with no parent) there is no target.
                base_class = self._base_class_of(function_node)
                return f"{base_class}.{method}" if base_class else None
            class_name = self._simple_type_name(self._get_node_text_simple(function_node))
            return f"{class_name}.{method}"

        # new Class(...)
        if parent_type == "object_creation_expression":
            return self._class_name_in(parent)

        # plain function call: foo(...)
        if function_node.type in ("name", "qualified_name"):
            return self._simple_type_name(self._get_node_text_simple(function_node))

        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from PHP syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def process_children(parent_node: Any, parent_component: Optional[str]) -> None:
            """Walk a node's children, threading PHP namespace scope.

            PHP namespaces come in two forms:
              - ``namespace App\\Foo;``     applies to all following siblings
              - ``namespace App\\Foo { }``  applies only within its braced body
            The namespace (dotted) replaces the file stem as the top-level id
            prefix, so members stay unique across namespaces (mirrors C#).
            """
            current_parent = parent_component
            for child in parent_node.children:
                if child.type == "namespace_definition":
                    ns_name = self._namespace_name(child, source_lines)
                    body = next((c for c in child.children if c.type == "compound_statement"), None)
                    if body is not None:
                        process_children(body, ns_name or parent_component)
                    else:
                        current_parent = ns_name or parent_component
                    continue
                extract_from_node(child, current_parent)

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            if node.type == "function_definition":
                component = self._extract_function_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            elif node.type == "method_declaration":
                component = self._extract_method_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            elif node.type in _CONTAINER_NODE_TYPES:
                component = self._extract_container_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    for child in node.children:
                        if child.type in _BODY_NODE_TYPES:
                            for grandchild in child.children:
                                extract_from_node(grandchild, component.id)
                # Container members are handled above; don't double-visit.
                return

            elif node.type == "const_declaration":
                components.extend(
                    self._extract_const_components(node, source_lines, file_path, parent_component)
                )

            elif node.type == "property_declaration":
                components.extend(
                    self._extract_property_components(
                        node, source_lines, file_path, parent_component
                    )
                )

            elif node.type == "enum_case":
                component = self._extract_enum_case_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            # Recurse into other nodes (php tags, statements, nested functions,
            # etc.). Containers already recursed and returned above.
            for child in node.children:
                extract_from_node(child, parent_component)

        process_children(root_node, None)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from a PHP function/method node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type != "formal_parameters":
                    continue
                for param_child in child.children:
                    if param_child.type in (
                        "simple_parameter",
                        "property_promotion_parameter",
                    ):
                        name = self._parameter_name(param_child, source_lines)
                        if name:
                            required = not self._parameter_has_default(param_child)
                            parameters.append(Parameter(name=name, required=required))
                    elif param_child.type == "variadic_parameter":
                        name = self._parameter_name(param_child, source_lines)
                        if name:
                            parameters.append(Parameter(name=name, required=False))
        except Exception:
            pass

        return parameters

    # ------------------------------------------------------------------
    # Component extractors
    # ------------------------------------------------------------------

    def _extract_function_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        try:
            name = self._child_name(node, source_lines)
            if not name:
                return None

            component_id = self._build_id(file_path, name, parent_component)
            parameters = self.extract_function_parameters(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                parameters=parameters,
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.FUNCTION,
                node,
                file_path,
                "php",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_method_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        try:
            name = self._child_name(node, source_lines)
            if not name:
                return None

            component_id = self._build_id(file_path, name, parent_component)
            parameters = self.extract_function_parameters(node, source_lines)
            is_constructor = name == "__construct"

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                parameters=parameters,
                visibility=self._extract_visibility(node, source_lines),
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=self._has_modifier(node, "static_modifier"),
                is_abstract=self._has_modifier(node, "abstract_modifier"),
                is_final=self._has_modifier(node, "final_modifier"),
            )

            component_type = ComponentType.CONSTRUCTOR if is_constructor else ComponentType.METHOD

            return self._create_component(
                component_id, component_type, node, file_path, "php", attributes, parent_component
            )
        except Exception:
            return None

    def _extract_container_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        try:
            name = self._child_name(node, source_lines)
            if not name:
                return None

            component_id = self._build_id(file_path, name, parent_component)
            component_type = self._determine_container_type(node)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_abstract=self._has_modifier(node, "abstract_modifier"),
                is_final=self._has_modifier(node, "final_modifier"),
            )

            return self._create_component(
                component_id, component_type, node, file_path, "php", attributes, parent_component
            )
        except Exception:
            return None

    def _extract_const_components(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> List[Component]:
        components: List[Component] = []
        try:
            for child in node.children:
                if child.type != "const_element":
                    continue
                name = self._child_name(child, source_lines)
                if not name:
                    continue
                component_id = self._build_id(file_path, name, parent_component)
                attributes = ComponentAttributes(
                    signature=self._extract_signature(node, source_lines),
                    visibility=self._extract_visibility(node, source_lines),
                    line_count=node.end_point[0] - node.start_point[0] + 1,
                )
                components.append(
                    self._create_component(
                        component_id,
                        ComponentType.CONSTANT,
                        node,
                        file_path,
                        "php",
                        attributes,
                        parent_component,
                    )
                )
        except Exception:
            pass
        return components

    def _extract_property_components(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> List[Component]:
        components: List[Component] = []
        try:
            visibility = self._extract_visibility(node, source_lines)
            is_static = self._has_modifier(node, "static_modifier")
            for child in node.children:
                if child.type != "property_element":
                    continue
                name = self._variable_name_in(child)
                if not name:
                    continue
                component_id = self._build_id(file_path, name, parent_component)
                attributes = ComponentAttributes(
                    signature=self._extract_signature(node, source_lines),
                    visibility=visibility,
                    line_count=node.end_point[0] - node.start_point[0] + 1,
                    is_static=is_static,
                )
                components.append(
                    self._create_component(
                        component_id,
                        ComponentType.VARIABLE,
                        node,
                        file_path,
                        "php",
                        attributes,
                        parent_component,
                    )
                )
        except Exception:
            pass
        return components

    def _extract_enum_case_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        try:
            name = self._child_name(node, source_lines)
            if not name:
                return None
            component_id = self._build_id(file_path, name, parent_component)
            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )
            return self._create_component(
                component_id,
                ComponentType.CONSTANT,
                node,
                file_path,
                "php",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_id(self, file_path: Path, name: str, parent_component: Optional[str]) -> str:
        # Namespace (when present) is threaded in as parent_component; otherwise
        # the scheme's file prefix applies. Delegates to the shared id builder.
        return self.make_id(file_path, name, parent_component)

    def _child_name(self, node: Any, source_lines: List[str]) -> Optional[str]:
        for child in node.children:
            if child.type == "name":
                return self._get_node_text(child, source_lines)
        return None

    def _namespace_name(self, namespace_def_node: Any, source_lines: List[str]) -> Optional[str]:
        """Return the dotted namespace from a namespace_definition (``App\\Foo`` -> ``App.Foo``)."""
        for child in namespace_def_node.children:
            if child.type == "namespace_name":
                text = self._get_node_text(child, source_lines)
                if text and text != "unknown":
                    return text.replace("\\", ".")
        return None

    def _variable_name_in(self, node: Any) -> Optional[str]:
        """Extract the plain (no ``$``) identifier from a node containing a variable_name."""
        for child in node.children:
            if child.type == "variable_name":
                return self._variable_name(child)
        return None

    def _variable_name(self, variable_name_node: Any) -> Optional[str]:
        """Return the identifier of a variable_name node without the leading ``$``."""
        for child in variable_name_node.children:
            if child.type == "name":
                return self._get_node_text_simple(child)
        text = self._get_node_text_simple(variable_name_node)
        return text.lstrip("$") if text and text != "unknown" else None

    def _method_name_after_operator(self, call_node: Any) -> Optional[str]:
        """Return the method name appearing after ``->``/``::`` in a call node."""
        seen_operator = False
        for child in call_node.children:
            if child.type in ("->", "?->", "::"):
                seen_operator = True
                continue
            if seen_operator and child.type == "name":
                return self._get_node_text_simple(child)
        return None

    def _class_name_in(self, object_creation_node: Any) -> Optional[str]:
        for child in object_creation_node.children:
            if child.type in ("name", "qualified_name"):
                return self._simple_type_name(self._get_node_text_simple(child))
        return None

    def _base_class_of(self, node: Any) -> Optional[str]:
        """Simple name of the class in the ``extends`` clause of the enclosing class."""
        while node is not None and node.type not in ("class_declaration", "anonymous_class"):
            node = node.parent
        if node is None:
            return None

        for child in node.children:
            if child.type == "base_clause":
                return self._class_name_in(child)
        return None

    def _simple_type_name(self, text: Optional[str]) -> Optional[str]:
        """Strip a PHP namespace prefix (``Foo\\Bar`` -> ``Bar``)."""
        if not text or text == "unknown":
            return None
        return text.split("\\")[-1]

    def _parameter_name(self, param_node: Any, source_lines: List[str]) -> Optional[str]:
        for child in param_node.children:
            if child.type == "variable_name":
                return self._variable_name(child)
        return None

    def _parameter_has_default(self, param_node: Any) -> bool:
        return any(child.type == "=" for child in param_node.children)

    def _extract_visibility(self, node: Any, source_lines: List[str]) -> Visibility:
        for child in node.children:
            if child.type == "visibility_modifier":
                text = self._get_node_text(child, source_lines).lower()
                if "private" in text:
                    return Visibility.PRIVATE
                if "protected" in text:
                    return Visibility.PROTECTED
                if "public" in text:
                    return Visibility.PUBLIC
        # PHP members default to public visibility.
        return Visibility.PUBLIC

    def _has_modifier(self, node: Any, modifier_type: str) -> bool:
        return any(child.type == modifier_type for child in node.children)

    def _extract_signature(self, node: Any, source_lines: List[str]) -> str:
        text = self._get_node_text(node, source_lines)
        lines = text.split("\n")
        if len(lines) > 1:
            return lines[0][:200]
        return text[:200]

    def _determine_container_type(self, node: Any) -> ComponentType:
        if node.type == "interface_declaration":
            return ComponentType.INTERFACE
        if node.type == "enum_declaration":
            return ComponentType.ENUM
        # trait_declaration and class_declaration both map to CLASS.
        return ComponentType.CLASS
