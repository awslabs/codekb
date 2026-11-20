"""
JavaScript-specific parser implementation.

Covers legacy (ES3/ES5) through modern (ES2022+) JavaScript patterns:
- Function declarations, expressions, arrow functions, generators, async
- Classes (ES6+), prototype-based classes (legacy)
- Object literal methods
- Export/CommonJS patterns (unwrapped to underlying type)
- Nested/inline named functions
- Class fields, private fields, static fields
- Destructured, rest, and default parameters
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


class JavaScriptParser(BaseLanguageParser):
    """JavaScript-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in JavaScript."""
        return ["call_expression", "new_expression"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in JavaScript."""
        return [
            "function_declaration",
            "arrow_function",
            "function_expression",
            "method_definition",
            "generator_function_declaration",
        ]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from JavaScript call node."""
        if function_node.type == "identifier":
            return self._get_node_text_simple(function_node)
        elif function_node.type == "member_expression":
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._get_node_text_simple(object_node)
                method_name = self._get_node_text_simple(method_name_node)
                return f"{object_name}.{method_name}"
        elif function_node.type == "subscript_expression":
            return None
        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from JavaScript syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            # --- Function declarations (including generators) ---
            if node.type in ["function_declaration", "generator_function_declaration"]:
                component = self._extract_function_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    # Recurse into function body for nested named functions
                    self._extract_nested_functions(
                        node, source_lines, file_path, component.id, components
                    )
                return  # Don't recurse children normally — handled above

            # --- Class declarations ---
            if node.type == "class_declaration":
                component = self._extract_class_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    self._extract_class_members(
                        node, source_lines, file_path, component.id, components
                    )
                return

            # --- Export statements: unwrap to underlying declaration ---
            if node.type == "export_statement":
                self._extract_export_statement(
                    node, source_lines, file_path, parent_component, components
                )
                return

            # --- Variable/lexical declarations ---
            if node.type in ["variable_declaration", "lexical_declaration"]:
                self._extract_variable_declarations(
                    node, source_lines, file_path, parent_component, components
                )
                return

            # --- Expression statements (prototype assignments, module.exports, etc.) ---
            if node.type == "expression_statement":
                self._extract_expression_statement(
                    node, source_lines, file_path, parent_component, components
                )
                return

            # --- Recurse into children for other node types ---
            for child in node.children:
                extract_from_node(child, parent_component)

        extract_from_node(root_node)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from JavaScript function definition node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "formal_parameters":
                    for param_child in child.children:
                        param = self._extract_single_parameter(param_child, source_lines)
                        if param:
                            parameters.append(param)
        except Exception:
            pass

        return parameters

    def _extract_single_parameter(
        self, param_node: Any, source_lines: List[str]
    ) -> Optional[Parameter]:
        """Extract a single parameter from a formal_parameters child node."""
        if param_node.type == "identifier":
            return Parameter(name=self._get_node_text(param_node, source_lines), required=True)
        elif param_node.type == "assignment_pattern":
            # Default parameter: param = value
            for subchild in param_node.children:
                if subchild.type == "identifier":
                    return Parameter(
                        name=self._get_node_text(subchild, source_lines), required=False
                    )
                elif subchild.type == "object_pattern":
                    return Parameter(
                        name=self._get_node_text(subchild, source_lines), required=False
                    )
                elif subchild.type == "array_pattern":
                    return Parameter(
                        name=self._get_node_text(subchild, source_lines), required=False
                    )
            return None
        elif param_node.type == "rest_pattern":
            for subchild in param_node.children:
                if subchild.type == "identifier":
                    name = self._get_node_text(subchild, source_lines)
                    return Parameter(name=f"...{name}", required=False)
            return None
        elif param_node.type == "object_pattern":
            return Parameter(name=self._get_node_text(param_node, source_lines), required=True)
        elif param_node.type == "array_pattern":
            return Parameter(name=self._get_node_text(param_node, source_lines), required=True)
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Function extraction
    # ─────────────────────────────────────────────────────────────────────

    def _extract_function_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
    ) -> Optional[Component]:
        """Extract a function declaration or generator function declaration."""
        try:
            function_name = "anonymous"
            for child in node.children:
                if child.type == "identifier":
                    function_name = self._get_node_text(child, source_lines)
                    break

            if function_name == "anonymous":
                return None

            component_id = self.make_id(file_path, function_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)
            visibility = Visibility.PRIVATE if function_name.startswith("_") else Visibility.PUBLIC

            is_async = any(child.type == "async" for child in node.children)
            is_generator = node.type == "generator_function_declaration"

            decorators: List[str] = []
            if is_async:
                decorators.append("async")
            if is_generator:
                decorators.append("generator")

            # Heuristic: detect constructor functions (legacy class pattern)
            # A function is likely a constructor if its name is PascalCase
            # and it uses `this.x = ...` inside its body
            is_constructor_function = function_name[
                0
            ].isupper() and self._body_uses_this_assignment(node)

            if is_constructor_function:
                component_type = ComponentType.CLASS
            else:
                component_type = (
                    ComponentType.METHOD if parent_component else ComponentType.FUNCTION
                )

            attributes = ComponentAttributes(
                signature=self._build_signature(node, source_lines),
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                decorators=decorators if decorators else None,
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "javascript",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _body_uses_this_assignment(self, node: Any) -> bool:
        """Check if a function body contains `this.x = ...` patterns (constructor heuristic)."""
        try:
            for child in node.children:
                if child.type == "statement_block":
                    return self._has_this_assignment(child)
        except Exception:
            pass
        return False

    def _has_this_assignment(self, node: Any) -> bool:
        """Recursively check for this.x = ... assignment patterns."""
        if node.type == "assignment_expression":
            left = node.children[0] if node.children else None
            if left and left.type == "member_expression":
                obj = left.children[0] if left.children else None
                if obj and self._get_node_text_simple(obj) == "this":
                    return True
        for child in node.children:
            if self._has_this_assignment(child):
                return True
        return False

    def _extract_nested_functions(
        self,
        function_node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_id: str,
        components: List[Component],
    ) -> None:
        """Extract named nested functions from within a function body."""
        for child in function_node.children:
            if child.type == "statement_block":
                self._walk_for_nested_functions(
                    child, source_lines, file_path, parent_id, components
                )

    def _walk_for_nested_functions(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_id: str,
        components: List[Component],
    ) -> None:
        """Walk a block looking for nested named function declarations and const fn = ...."""
        for child in node.children:
            if child.type in ["function_declaration", "generator_function_declaration"]:
                comp = self._extract_function_component(child, source_lines, file_path, parent_id)
                if comp:
                    components.append(comp)
                    # Recurse deeper
                    self._extract_nested_functions(
                        child, source_lines, file_path, comp.id, components
                    )
            elif child.type in ["variable_declaration", "lexical_declaration"]:
                self._extract_variable_declarations(
                    child, source_lines, file_path, parent_id, components
                )
            elif child.type == "expression_statement":
                # Could be a named function expression assignment inside a function
                self._extract_expression_statement(
                    child, source_lines, file_path, parent_id, components
                )

    # ─────────────────────────────────────────────────────────────────────
    # Class extraction (ES6+ classes)
    # ─────────────────────────────────────────────────────────────────────

    def _extract_class_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
    ) -> Optional[Component]:
        """Extract an ES6 class declaration."""
        try:
            name = "unknown"
            superclass = None
            for child in node.children:
                if child.type == "identifier":
                    name = self._get_node_text(child, source_lines)
                elif child.type == "class_heritage":
                    for heritage_child in child.children:
                        if heritage_child.type == "identifier":
                            superclass = self._get_node_text(heritage_child, source_lines)
                        elif heritage_child.type == "call_expression":
                            # Mixin pattern: class Foo extends Mixin(Base)
                            superclass = self._get_node_text(heritage_child, source_lines)

            component_id = self.make_id(file_path, name, parent_component)

            sig = f"class {name}"
            if superclass:
                sig += f" extends {superclass}"

            attributes = ComponentAttributes(
                signature=sig,
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.CLASS,
                node,
                file_path,
                "javascript",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_class_members(
        self,
        class_node: Any,
        source_lines: List[str],
        file_path: Path,
        class_id: str,
        components: List[Component],
    ) -> None:
        """Extract all members from a class body."""
        for child in class_node.children:
            if child.type == "class_body":
                for member in child.children:
                    self._extract_single_class_member(
                        member, source_lines, file_path, class_id, components
                    )

    def _extract_single_class_member(
        self,
        member: Any,
        source_lines: List[str],
        file_path: Path,
        class_id: str,
        components: List[Component],
    ) -> None:
        """Extract a single class member (method, field, getter, setter, etc.)."""
        if member.type == "method_definition":
            comp = self._extract_method_component(member, source_lines, file_path, class_id)
            if comp:
                components.append(comp)

        elif member.type in ["field_definition", "public_field_definition"]:
            comp = self._extract_class_field(member, source_lines, file_path, class_id)
            if comp:
                components.append(comp)

    def _extract_method_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        class_id: str,
    ) -> Optional[Component]:
        """Extract a class method (including constructor, getter, setter, static, private)."""
        try:
            method_name = "anonymous"
            is_static = False
            is_getter = False
            is_setter = False
            is_async = False
            is_generator = False
            is_private = False

            for child in node.children:
                if child.type == "property_identifier":
                    method_name = self._get_node_text(child, source_lines)
                elif child.type == "private_property_identifier":
                    method_name = self._get_node_text(child, source_lines)
                    is_private = True
                elif child.type == "identifier":
                    method_name = self._get_node_text(child, source_lines)
                elif child.type == "computed_property_name":
                    method_name = self._get_node_text(child, source_lines)
                elif child.type == "static":
                    is_static = True
                elif child.type == "get":
                    is_getter = True
                elif child.type == "set":
                    is_setter = True
                elif child.type == "async":
                    is_async = True
                elif child.type == "*":
                    is_generator = True

            # Determine if this is a constructor
            if method_name == "constructor":
                component_type = ComponentType.CONSTRUCTOR
            else:
                component_type = ComponentType.METHOD

            component_id = f"{class_id}.{method_name}"

            parameters = self.extract_function_parameters(node, source_lines)

            if is_private or method_name.startswith("#"):
                visibility = Visibility.PRIVATE
            else:
                visibility = Visibility.PUBLIC

            decorators: List[str] = []
            if is_static:
                decorators.append("static")
            if is_async:
                decorators.append("async")
            if is_generator:
                decorators.append("generator")
            if is_getter:
                decorators.append("getter")
            if is_setter:
                decorators.append("setter")

            attributes = ComponentAttributes(
                signature=self._build_signature(node, source_lines),
                parameters=parameters,
                visibility=visibility,
                is_static=is_static,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                decorators=decorators if decorators else None,
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "javascript",
                attributes,
                class_id,
            )
        except Exception:
            return None

    def _extract_class_field(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        class_id: str,
    ) -> Optional[Component]:
        """Extract a class field (instance, static, or private)."""
        try:
            field_name = None
            is_static = False
            is_private = False
            is_function = False
            is_async = False
            function_node = None

            for child in node.children:
                if child.type == "property_identifier":
                    field_name = self._get_node_text(child, source_lines)
                elif child.type == "private_property_identifier":
                    field_name = self._get_node_text(child, source_lines)
                    is_private = True
                elif child.type == "static":
                    is_static = True
                elif child.type in ["arrow_function", "function_expression"]:
                    is_function = True
                    function_node = child
                    # Check for async inside the function node
                    for sub in child.children:
                        if sub.type == "async":
                            is_async = True

            if not field_name:
                return None

            component_id = f"{class_id}.{field_name}"

            if is_function:
                component_type = ComponentType.METHOD
                parameters = (
                    self.extract_function_parameters(function_node, source_lines)
                    if function_node
                    else []
                )
            else:
                component_type = ComponentType.VARIABLE
                parameters = []

            visibility = Visibility.PRIVATE if is_private else Visibility.PUBLIC

            decorators: List[str] = []
            if is_static:
                decorators.append("static")
            if is_async:
                decorators.append("async")

            attributes = ComponentAttributes(
                signature=self._get_node_text(node, source_lines)[:100],
                parameters=parameters,
                visibility=visibility,
                is_static=is_static,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                decorators=decorators if decorators else None,
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "javascript",
                attributes,
                class_id,
            )
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────
    # Variable declarations (const/let/var with function or value)
    # ─────────────────────────────────────────────────────────────────────

    def _extract_variable_declarations(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
        components: List[Component],
    ) -> None:
        """Extract components from variable/lexical declarations."""
        is_const = any(
            child.type == "const" or self._get_node_text_simple(child) == "const"
            for child in node.children
            if child.type in ["const", "let", "var"]
        )

        for child in node.children:
            if child.type == "variable_declarator":
                comp = self._extract_from_declarator(
                    child, node, source_lines, file_path, parent_component, is_const
                )
                if comp:
                    components.append(comp)
                    # If it's a function, look for nested functions inside
                    if comp.type in [ComponentType.FUNCTION, ComponentType.METHOD]:
                        self._extract_nested_from_declarator(
                            child, source_lines, file_path, comp.id, components
                        )
                    # If it's a class expression, extract its members
                    if comp.type == ComponentType.CLASS:
                        for sub in child.children:
                            if sub.type == "class":
                                self._extract_class_members(
                                    sub, source_lines, file_path, comp.id, components
                                )
                    # If it's an object literal (CONSTANT), extract methods
                    if comp.type in [ComponentType.CONSTANT, ComponentType.VARIABLE]:
                        for sub in child.children:
                            if sub.type == "object":
                                self._extract_object_methods(
                                    sub, source_lines, file_path, comp.id, components
                                )

    def _extract_from_declarator(
        self,
        declarator: Any,
        declaration_node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
        is_const: bool,
    ) -> Optional[Component]:
        """Extract a component from a single variable_declarator."""
        try:
            variable_name = None
            value_node = None

            for child in declarator.children:
                if child.type == "identifier":
                    variable_name = self._get_node_text(child, source_lines)
                elif child.type in [
                    "arrow_function",
                    "function_expression",
                    "call_expression",
                    "class",
                ]:
                    value_node = child
                elif child.type == "object":
                    # Object literal — check for methods inside
                    value_node = child

            if not variable_name:
                return None

            # Determine if this is a function assignment
            is_function_value = False
            if value_node and value_node.type in ["arrow_function", "function_expression"]:
                is_function_value = True
            elif value_node and value_node.type == "call_expression":
                is_function_value = self._contains_function_in_call(value_node)
            elif value_node and value_node.type == "class":
                is_function_value = True  # Treat class expressions as significant

            # Skip private/internal variables (leading underscore) — but keep functions
            if variable_name.startswith("_") and not is_function_value:
                return None

            # If inside a function (parent_component set), only extract function assignments
            # Skip plain const/let/var that are just local variables
            if parent_component and not is_function_value:
                return None

            component_id = self.make_id(file_path, variable_name, parent_component)

            # Determine component type based on value
            if value_node and value_node.type in ["arrow_function", "function_expression"]:
                component_type = (
                    ComponentType.METHOD if parent_component else ComponentType.FUNCTION
                )
                parameters = self.extract_function_parameters(value_node, source_lines)
                is_async = any(child.type == "async" for child in value_node.children)
                decorators = ["async"] if is_async else None
            elif value_node and value_node.type == "call_expression":
                # e.g., const App = memo(function() {}), const handler = withAuth(fn)
                if self._contains_function_in_call(value_node):
                    component_type = (
                        ComponentType.METHOD if parent_component else ComponentType.FUNCTION
                    )
                    parameters = self._extract_params_from_call_arg(value_node, source_lines)
                    decorators = None
                else:
                    component_type = ComponentType.CONSTANT if is_const else ComponentType.VARIABLE
                    parameters = []
                    decorators = None
            elif value_node and value_node.type == "class":
                # const Foo = class { ... } or const Foo = class Bar { ... }
                component_type = ComponentType.CLASS
                parameters = []
                decorators = None
            elif value_node and value_node.type == "object":
                # Object with methods — extract as CONSTANT, methods extracted separately
                component_type = ComponentType.CONSTANT if is_const else ComponentType.VARIABLE
                parameters = []
                decorators = None
                # Object methods will be extracted after component creation below
            else:
                component_type = ComponentType.CONSTANT if is_const else ComponentType.VARIABLE
                parameters = []
                decorators = None

            visibility = Visibility.PRIVATE if variable_name.startswith("_") else Visibility.PUBLIC

            attributes = ComponentAttributes(
                signature=self._build_signature(declaration_node, source_lines),
                parameters=parameters,
                visibility=visibility,
                line_count=declaration_node.end_point[0] - declaration_node.start_point[0] + 1,
                decorators=decorators,
            )

            comp = self._create_component(
                component_id,
                component_type,
                declaration_node,
                file_path,
                "javascript",
                attributes,
                parent_component,
            )

            # If it's a class expression, also extract its members
            if value_node and value_node.type == "class" and comp:
                # Return comp; caller will handle member extraction
                pass

            # If it's an object literal, extract methods as children
            if value_node and value_node.type == "object" and comp:
                # Return comp; caller will handle method extraction
                pass

            return comp
        except Exception:
            return None

    def _extract_nested_from_declarator(
        self,
        declarator: Any,
        source_lines: List[str],
        file_path: Path,
        parent_id: str,
        components: List[Component],
    ) -> None:
        """Extract nested named functions from inside a function assigned to a variable."""
        for child in declarator.children:
            if child.type in ["arrow_function", "function_expression"]:
                for sub in child.children:
                    if sub.type == "statement_block":
                        self._walk_for_nested_functions(
                            sub, source_lines, file_path, parent_id, components
                        )

    def _extract_class_expression_members(
        self,
        class_node: Any,
        source_lines: List[str],
        file_path: Path,
        class_id: str,
        components: List[Component],
    ) -> None:
        """Extract members from a class expression (const Foo = class { ... })."""
        self._extract_class_members(class_node, source_lines, file_path, class_id, components)

    # ─────────────────────────────────────────────────────────────────────
    # Export statements (unwrap to underlying declaration)
    # ─────────────────────────────────────────────────────────────────────

    def _extract_export_statement(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
        components: List[Component],
    ) -> None:
        """Unwrap export statements and extract the underlying declaration."""
        for child in node.children:
            if child.type == "function_declaration":
                comp = self._extract_function_component(
                    child, source_lines, file_path, parent_component
                )
                if comp:
                    components.append(comp)
                    self._extract_nested_functions(
                        child, source_lines, file_path, comp.id, components
                    )
            elif child.type == "generator_function_declaration":
                comp = self._extract_function_component(
                    child, source_lines, file_path, parent_component
                )
                if comp:
                    components.append(comp)
            elif child.type == "class_declaration":
                comp = self._extract_class_component(
                    child, source_lines, file_path, parent_component
                )
                if comp:
                    components.append(comp)
                    self._extract_class_members(child, source_lines, file_path, comp.id, components)
            elif child.type in ["variable_declaration", "lexical_declaration"]:
                self._extract_variable_declarations(
                    child, source_lines, file_path, parent_component, components
                )
            elif child.type == "class":
                # export default class { ... }
                # Try to get name from the class node itself
                name = self._get_class_expression_name(child, source_lines)
                if not name:
                    name = "default"
                comp_id = self.make_id(file_path, name)
                attributes = ComponentAttributes(
                    signature=f"export default class {name}",
                    visibility=Visibility.PUBLIC,
                    line_count=child.end_point[0] - child.start_point[0] + 1,
                )
                comp = self._create_component(
                    comp_id,
                    ComponentType.CLASS,
                    child,
                    file_path,
                    "javascript",
                    attributes,
                    parent_component,
                )
                if comp:
                    components.append(comp)
                    self._extract_class_members(child, source_lines, file_path, comp.id, components)
            elif child.type in ["function_expression", "arrow_function"]:
                # export default function() {} or export default () => {}
                # Try to extract name from function_expression
                name = None
                for sub in child.children:
                    if sub.type == "identifier":
                        name = self._get_node_text(sub, source_lines)
                        break
                if not name:
                    name = "default"
                comp_id = self.make_id(file_path, name)
                parameters = self.extract_function_parameters(child, source_lines)
                attributes = ComponentAttributes(
                    signature=self._build_signature(child, source_lines),
                    parameters=parameters,
                    visibility=Visibility.PUBLIC,
                    line_count=child.end_point[0] - child.start_point[0] + 1,
                )
                comp = self._create_component(
                    comp_id,
                    ComponentType.FUNCTION,
                    child,
                    file_path,
                    "javascript",
                    attributes,
                    parent_component,
                )
                if comp:
                    components.append(comp)

    # ─────────────────────────────────────────────────────────────────────
    # Expression statements (prototype, module.exports, exports.x, etc.)
    # ─────────────────────────────────────────────────────────────────────

    def _extract_expression_statement(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
        components: List[Component],
    ) -> None:
        """Extract components from expression statements."""
        if not node.children:
            return

        expr = node.children[0]

        if expr.type == "assignment_expression":
            self._extract_assignment_expression(
                expr, source_lines, file_path, parent_component, components
            )
        elif expr.type == "call_expression":
            # Handle Object.assign(Foo.prototype, { method() {} })
            self._extract_object_assign_prototype(expr, source_lines, file_path, components)

    def _extract_assignment_expression(
        self,
        expr: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
        components: List[Component],
    ) -> None:
        """Extract from assignment expressions (prototype methods, exports, etc.)."""
        if len(expr.children) < 3:
            return

        left = expr.children[0]
        right = expr.children[2]

        # Pattern: Foo.prototype.method = function() {}
        if left.type == "member_expression" and self._is_prototype_assignment(left):
            self._extract_prototype_method(left, right, source_lines, file_path, components)
            return

        # Pattern: Foo.prototype = { method1() {}, method2() {} }
        if left.type == "member_expression" and self._is_prototype_direct_assignment(left):
            if right.type == "object":
                class_name = self._get_prototype_class_name(left)
                if class_name:
                    class_id = self.make_id(file_path, class_name)
                    self._extract_object_methods(
                        right, source_lines, file_path, class_id, components
                    )
            return

        # Pattern: module.exports = { ... } or module.exports = function() {}
        left_text = self._get_node_text_simple(left)
        if left_text == "module.exports":
            self._extract_module_exports(right, source_lines, file_path, components)
            return

        # Pattern: exports.foo = function() {}
        if (
            left.type == "member_expression"
            and left.children
            and self._get_node_text_simple(left.children[0]) == "exports"
        ):
            self._extract_exports_member(left, right, source_lines, file_path, components)
            return

        # Pattern: SomeObj.method = function() {} (static method assignment)
        if left.type == "member_expression" and right.type in [
            "function_expression",
            "arrow_function",
        ]:
            self._extract_static_method_assignment(
                left, right, source_lines, file_path, parent_component, components
            )

    def _is_prototype_assignment(self, member_expr: Any) -> bool:
        """Check if a member expression is a prototype method assignment (Foo.prototype.bar)."""
        text = self._get_node_text_simple(member_expr)
        return ".prototype." in text

    def _is_prototype_direct_assignment(self, member_expr: Any) -> bool:
        """Check if a member expression is Foo.prototype (direct prototype replacement)."""
        text = self._get_node_text_simple(member_expr)
        return text.endswith(".prototype")

    def _get_prototype_class_name(self, member_expr: Any) -> Optional[str]:
        """Extract class name from Foo.prototype member expression."""
        text = self._get_node_text_simple(member_expr)
        if text.endswith(".prototype"):
            return text[: -len(".prototype")]
        return None

    def _extract_prototype_method(
        self,
        left: Any,
        right: Any,
        source_lines: List[str],
        file_path: Path,
        components: List[Component],
    ) -> None:
        """Extract Foo.prototype.method = function() {} as a METHOD on Foo."""
        try:
            left_text = self._get_node_text_simple(left)
            parts = left_text.split(".prototype.")
            if len(parts) != 2:
                return

            class_name = parts[0]
            method_name = parts[1]

            class_id = self.make_id(file_path, class_name)
            component_id = f"{class_id}.{method_name}"

            parameters: List[Parameter] = []
            if right.type in ["function_expression", "arrow_function"]:
                parameters = self.extract_function_parameters(right, source_lines)

            attributes = ComponentAttributes(
                signature=f"{class_name}.prototype.{method_name}",
                parameters=parameters,
                visibility=Visibility.PUBLIC,
                line_count=right.end_point[0] - right.start_point[0] + 1,
            )

            comp = self._create_component(
                component_id,
                ComponentType.METHOD,
                right,
                file_path,
                "javascript",
                attributes,
                class_id,
            )
            if comp:
                components.append(comp)
        except Exception:
            pass

    def _extract_object_assign_prototype(
        self,
        call_node: Any,
        source_lines: List[str],
        file_path: Path,
        components: List[Component],
    ) -> None:
        """Extract methods from Object.assign(Foo.prototype, { method() {} })."""
        try:
            # Verify this is Object.assign or Object.defineProperties
            if not call_node.children:
                return
            callee = call_node.children[0]
            callee_text = self._get_node_text_simple(callee)
            if callee_text not in ("Object.assign", "Object.defineProperties"):
                return

            # Find arguments
            for child in call_node.children:
                if child.type == "arguments":
                    args = [c for c in child.children if c.type not in ["(", ")", ","]]
                    if len(args) < 2:
                        return

                    # First arg should be Foo.prototype
                    first_arg = args[0]
                    first_arg_text = self._get_node_text_simple(first_arg)
                    if ".prototype" not in first_arg_text:
                        return

                    class_name = first_arg_text.split(".prototype")[0]
                    class_id = self.make_id(file_path, class_name)

                    # Second arg should be an object literal with methods
                    second_arg = args[1]
                    if second_arg.type == "object":
                        self._extract_object_methods(
                            second_arg, source_lines, file_path, class_id, components
                        )
                    return
        except Exception:
            pass

    def _extract_module_exports(
        self,
        right: Any,
        source_lines: List[str],
        file_path: Path,
        components: List[Component],
    ) -> None:
        """Extract from module.exports = { ... } or module.exports = function() {}."""
        if right.type == "object":
            # module.exports = { method1() {}, method2: function() {} }
            self._extract_object_methods(right, source_lines, file_path, self.file_prefix(file_path), components)
        elif right.type in ["function_expression", "arrow_function"]:
            # module.exports = function handler() {}
            name = None
            for child in right.children:
                if child.type == "identifier":
                    name = self._get_node_text(child, source_lines)
                    break
            if name:
                comp_id = self.make_id(file_path, name)
                parameters = self.extract_function_parameters(right, source_lines)
                attributes = ComponentAttributes(
                    signature=self._build_signature(right, source_lines),
                    parameters=parameters,
                    visibility=Visibility.PUBLIC,
                    line_count=right.end_point[0] - right.start_point[0] + 1,
                )
                comp = self._create_component(
                    comp_id,
                    ComponentType.FUNCTION,
                    right,
                    file_path,
                    "javascript",
                    attributes,
                    None,
                )
                if comp:
                    components.append(comp)
        elif right.type == "class":
            name = self._get_class_expression_name(right, source_lines) or "default"
            comp_id = self.make_id(file_path, name)
            attributes = ComponentAttributes(
                signature=f"class {name}",
                visibility=Visibility.PUBLIC,
                line_count=right.end_point[0] - right.start_point[0] + 1,
            )
            comp = self._create_component(
                comp_id,
                ComponentType.CLASS,
                right,
                file_path,
                "javascript",
                attributes,
                None,
            )
            if comp:
                components.append(comp)
                self._extract_class_members(right, source_lines, file_path, comp.id, components)

    def _extract_exports_member(
        self,
        left: Any,
        right: Any,
        source_lines: List[str],
        file_path: Path,
        components: List[Component],
    ) -> None:
        """Extract exports.foo = function() {} as a FUNCTION named foo."""
        try:
            # Get the property name (exports.foo -> foo)
            if len(left.children) >= 3:
                prop_node = left.children[2]
                prop_name = self._get_node_text_simple(prop_node)
            else:
                return

            comp_id = self.make_id(file_path, prop_name)

            if right.type in ["function_expression", "arrow_function"]:
                parameters = self.extract_function_parameters(right, source_lines)
                attributes = ComponentAttributes(
                    signature=f"exports.{prop_name}",
                    parameters=parameters,
                    visibility=Visibility.PUBLIC,
                    line_count=right.end_point[0] - right.start_point[0] + 1,
                )
                comp = self._create_component(
                    comp_id,
                    ComponentType.FUNCTION,
                    right,
                    file_path,
                    "javascript",
                    attributes,
                    None,
                )
                if comp:
                    components.append(comp)
            elif right.type == "class":
                name = self._get_class_expression_name(right, source_lines) or prop_name
                attributes = ComponentAttributes(
                    signature=f"class {name}",
                    visibility=Visibility.PUBLIC,
                    line_count=right.end_point[0] - right.start_point[0] + 1,
                )
                comp = self._create_component(
                    comp_id,
                    ComponentType.CLASS,
                    right,
                    file_path,
                    "javascript",
                    attributes,
                    None,
                )
                if comp:
                    components.append(comp)
                    self._extract_class_members(right, source_lines, file_path, comp.id, components)
        except Exception:
            pass

    def _extract_static_method_assignment(
        self,
        left: Any,
        right: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
        components: List[Component],
    ) -> None:
        """Extract Obj.method = function() {} as a static method."""
        try:
            if len(left.children) < 3:
                return
            obj_name = self._get_node_text_simple(left.children[0])
            method_name = self._get_node_text_simple(left.children[2])

            # Skip prototype assignments (handled separately)
            if method_name == "prototype":
                return

            obj_id = self.make_id(file_path, obj_name)
            comp_id = f"{obj_id}.{method_name}"

            parameters = self.extract_function_parameters(right, source_lines)
            attributes = ComponentAttributes(
                signature=f"{obj_name}.{method_name}",
                parameters=parameters,
                visibility=Visibility.PUBLIC,
                is_static=True,
                line_count=right.end_point[0] - right.start_point[0] + 1,
                decorators=["static"],
            )

            comp = self._create_component(
                comp_id,
                ComponentType.METHOD,
                right,
                file_path,
                "javascript",
                attributes,
                obj_id,
            )
            if comp:
                components.append(comp)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # Object literal methods
    # ─────────────────────────────────────────────────────────────────────

    def _extract_object_methods(
        self,
        object_node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_id: str,
        components: List[Component],
    ) -> None:
        """Extract methods from object literals (shorthand and property functions)."""
        for child in object_node.children:
            if child.type == "method_definition":
                # Shorthand method: { method() {} }
                comp = self._extract_object_method(child, source_lines, file_path, parent_id)
                if comp:
                    components.append(comp)
            elif child.type == "pair":
                # Property function: { method: function() {} }
                comp = self._extract_pair_method(child, source_lines, file_path, parent_id)
                if comp:
                    components.append(comp)

    def _extract_object_method(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_id: str,
    ) -> Optional[Component]:
        """Extract a shorthand method from an object literal."""
        try:
            method_name = None
            is_async = False
            is_generator = False
            is_getter = False
            is_setter = False

            for child in node.children:
                if child.type in ["property_identifier", "identifier"]:
                    method_name = self._get_node_text(child, source_lines)
                elif child.type == "computed_property_name":
                    method_name = self._get_node_text(child, source_lines)
                elif child.type == "async":
                    is_async = True
                elif child.type == "*":
                    is_generator = True
                elif child.type == "get":
                    is_getter = True
                elif child.type == "set":
                    is_setter = True

            if not method_name:
                return None

            comp_id = f"{parent_id}.{method_name}"
            parameters = self.extract_function_parameters(node, source_lines)

            decorators: List[str] = []
            if is_async:
                decorators.append("async")
            if is_generator:
                decorators.append("generator")
            if is_getter:
                decorators.append("getter")
            if is_setter:
                decorators.append("setter")

            attributes = ComponentAttributes(
                signature=self._build_signature(node, source_lines),
                parameters=parameters,
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                decorators=decorators if decorators else None,
            )

            return self._create_component(
                comp_id,
                ComponentType.METHOD,
                node,
                file_path,
                "javascript",
                attributes,
                parent_id,
            )
        except Exception:
            return None

    def _extract_pair_method(
        self,
        pair_node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_id: str,
    ) -> Optional[Component]:
        """Extract { key: function() {} } from an object literal pair."""
        try:
            key_name = None
            value_node = None

            for child in pair_node.children:
                if child.type in ["property_identifier", "identifier", "string"]:
                    key_name = self._get_node_text(child, source_lines)
                    # Strip quotes from string keys
                    if key_name and key_name.startswith(("'", '"')):
                        key_name = key_name[1:-1]
                elif child.type in ["function_expression", "arrow_function"]:
                    value_node = child

            if not key_name or not value_node:
                return None

            comp_id = f"{parent_id}.{key_name}"
            parameters = self.extract_function_parameters(value_node, source_lines)

            attributes = ComponentAttributes(
                signature=f"{key_name}: function",
                parameters=parameters,
                visibility=Visibility.PUBLIC,
                line_count=value_node.end_point[0] - value_node.start_point[0] + 1,
            )

            return self._create_component(
                comp_id,
                ComponentType.METHOD,
                value_node,
                file_path,
                "javascript",
                attributes,
                parent_id,
            )
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────
    # Utility methods
    # ─────────────────────────────────────────────────────────────────────

    def _contains_function_in_call(self, call_node: Any) -> bool:
        """Check if a call expression contains a function as an argument."""
        try:
            for child in call_node.children:
                if child.type == "arguments":
                    for arg in child.children:
                        if arg.type in ["arrow_function", "function_expression"]:
                            return True
                        elif arg.type == "call_expression":
                            if self._contains_function_in_call(arg):
                                return True
            return False
        except Exception:
            return False

    def _extract_params_from_call_arg(
        self, call_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from the first function argument in a call expression."""
        try:
            for child in call_node.children:
                if child.type == "arguments":
                    for arg in child.children:
                        if arg.type in ["arrow_function", "function_expression"]:
                            return self.extract_function_parameters(arg, source_lines)
                        elif arg.type == "call_expression":
                            params = self._extract_params_from_call_arg(arg, source_lines)
                            if params:
                                return params
        except Exception:
            pass
        return []

    def _get_class_expression_name(self, class_node: Any, source_lines: List[str]) -> Optional[str]:
        """Get the name from a class expression (class Bar { ... })."""
        for child in class_node.children:
            if child.type == "identifier":
                return self._get_node_text(child, source_lines)
        return None

    def _build_signature(self, node: Any, source_lines: List[str]) -> str:
        """Build a truncated signature string from a node."""
        text = self._get_node_text(node, source_lines)
        # Take first line or first 100 chars
        first_line = text.split("\n")[0] if "\n" in text else text
        return first_line[:100]
