"""
Java-specific parser implementation.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


class JavaParser(BaseLanguageParser):
    """Java-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in Java."""
        return ["method_invocation"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in Java."""
        return ["method_declaration", "constructor_declaration"]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract the called name from a Java call node.

        The framework hands us the FIRST child of the ``method_invocation``
        node. In tree-sitter-java a ``recv.method(args)`` invocation lays the
        receiver, ``.``, method name, and arguments out as *sibling* children —
        so that first child is the receiver (``recv``), not the method. Reading
        it alone drops ``method`` and binds the call to whatever component
        happens to share the receiver's simple name. Resolve the real target
        from the enclosing ``method_invocation``'s ``object``/``name`` fields
        instead; fall back to the node itself only when there is no invocation
        (keeps bare identifiers and ``field_access`` working).
        """
        invocation = getattr(function_node, "parent", None)
        if invocation is not None and invocation.type == "method_invocation":
            name_node = invocation.child_by_field_name("name")
            if name_node is None:
                return None
            method_name = self._get_node_text_simple(name_node)
            object_node = invocation.child_by_field_name("object")
            if object_node is not None:
                return f"{self._get_node_text_simple(object_node)}.{method_name}"
            return method_name

        if function_node.type == "identifier":
            return self._get_node_text_simple(function_node)
        elif function_node.type == "field_access":
            # Java method call: obj.method()
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._get_node_text_simple(object_node)
                method_name = self._get_node_text_simple(method_name_node)
                return f"{object_name}.{method_name}"
        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from Java syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            if node.type in ["method_declaration", "constructor_declaration"]:
                component = self._extract_method_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            elif node.type in [
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
                "annotation_type_declaration",
            ]:
                component = self._extract_class_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    for child in node.children:
                        if child.type in [
                            "class_body",
                            "interface_body",
                            "enum_body",
                            "annotation_type_body",
                        ]:
                            for grandchild in child.children:
                                extract_from_node(grandchild, component.id)

            elif node.type == "field_declaration":
                component = self._extract_field_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            if node.type not in [
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
                "annotation_type_declaration",
            ]:
                for child in node.children:
                    extract_from_node(child, parent_component)

        extract_from_node(root_node)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from Java method/constructor node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "formal_parameters":
                    for param_child in child.children:
                        if param_child.type == "formal_parameter":
                            param_name = self._extract_parameter_name(param_child, source_lines)
                            if param_name:
                                parameters.append(Parameter(name=param_name, required=True))
                        elif param_child.type == "spread_parameter":
                            param_name = self._extract_parameter_name(param_child, source_lines)
                            if param_name:
                                parameters.append(Parameter(name=param_name, required=True))
        except Exception:
            pass

        return parameters

    def _extract_parameter_name(self, param_node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract parameter name from formal_parameter node."""
        for child in param_node.children:
            if child.type == "variable_declarator":
                for grandchild in child.children:
                    if grandchild.type == "identifier":
                        return self._get_node_text(grandchild, source_lines)
            elif child.type == "identifier":
                return self._get_node_text(child, source_lines)
        return None

    def _extract_method_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Java method/constructor component."""
        try:
            method_name = None
            is_constructor = node.type == "constructor_declaration"

            for child in node.children:
                if child.type == "identifier":
                    method_name = self._get_node_text(child, source_lines)
                    break

            if not method_name:
                return None

            component_id = self.make_id(file_path, method_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)
            visibility = self._extract_visibility(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=self._has_modifier(node, "static"),
                is_abstract=self._has_modifier(node, "abstract"),
                is_final=self._has_modifier(node, "final"),
            )

            component_type = ComponentType.CONSTRUCTOR if is_constructor else ComponentType.METHOD

            return self._create_component(
                component_id, component_type, node, file_path, "java", attributes, parent_component
            )
        except Exception:
            return None

    def _extract_class_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Java class/interface/enum/record component."""
        try:
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = self._get_node_text(child, source_lines)
                    break

            if not name:
                return None

            component_id = self.make_id(file_path, name, parent_component)

            component_type = self._determine_class_type(node)
            visibility = self._extract_visibility(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=self._has_modifier(node, "static"),
                is_abstract=self._has_modifier(node, "abstract"),
                is_final=self._has_modifier(node, "final"),
            )

            return self._create_component(
                component_id, component_type, node, file_path, "java", attributes, parent_component
            )
        except Exception:
            return None

    def _extract_field_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Java field/variable component."""
        try:
            field_name = None
            for child in node.children:
                if child.type == "variable_declarator":
                    for grandchild in child.children:
                        if grandchild.type == "identifier":
                            field_name = self._get_node_text(grandchild, source_lines)
                            break
                    if field_name:
                        break

            if not field_name:
                return None

            component_id = self.make_id(file_path, field_name, parent_component)

            visibility = self._extract_visibility(node, source_lines)
            is_constant = self._has_modifier(node, "static") and self._has_modifier(node, "final")

            component_type = ComponentType.CONSTANT if is_constant else ComponentType.VARIABLE

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=self._has_modifier(node, "static"),
                is_final=self._has_modifier(node, "final"),
            )

            return self._create_component(
                component_id, component_type, node, file_path, "java", attributes, parent_component
            )
        except Exception:
            return None

    def _extract_visibility(self, node: Any, source_lines: List[str]) -> Visibility:
        """Extract visibility modifier from node."""
        for child in node.children:
            if child.type == "modifiers":
                text = self._get_node_text(child, source_lines).lower()
                if "public" in text:
                    return Visibility.PUBLIC
                elif "protected" in text:
                    return Visibility.PROTECTED
                elif "private" in text:
                    return Visibility.PRIVATE
        return Visibility.PACKAGE

    def _has_modifier(self, node: Any, modifier: str) -> bool:
        """Check if node has specific modifier."""
        for child in node.children:
            if child.type == "modifiers":
                for mod_child in child.children:
                    if mod_child.type == modifier:
                        return True
        return False

    def _extract_signature(self, node: Any, source_lines: List[str]) -> str:
        """Extract method/class signature."""
        text = self._get_node_text(node, source_lines)
        lines = text.split("\n")
        if len(lines) > 1:
            return lines[0][:200]
        return text[:200]

    def _determine_class_type(self, node: Any) -> ComponentType:
        """Determine component type based on node type."""
        if node.type == "interface_declaration":
            return ComponentType.INTERFACE
        elif node.type == "enum_declaration":
            return ComponentType.ENUM
        elif node.type == "record_declaration":
            return ComponentType.CLASS
        elif node.type == "annotation_type_declaration":
            return ComponentType.INTERFACE
        return ComponentType.CLASS
