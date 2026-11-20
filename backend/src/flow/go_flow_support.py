"""Go-specific flow analysis support methods."""

from typing import Dict, List, Optional, Tuple, Any


def extract_go_parameter_types(
    node: Any, function_start_line: int, get_node_text_fn
) -> Dict[str, str]:
    """Extract parameter type annotations from Go function/method declaration."""
    param_types = {}

    if (
        node.type in ["function_declaration", "method_declaration"]
        and node.start_point[0] == function_start_line
    ):
        for child in node.children:
            if child.type == "parameter_list":
                for param_child in child.children:
                    if param_child.type == "parameter_declaration":
                        # Go groups params with same type: func foo(a, b int)
                        param_names = []
                        type_name = None
                        for subchild in param_child.children:
                            if subchild.type == "identifier":
                                param_names.append(get_node_text_fn(subchild))
                            elif subchild.type in [
                                "type_identifier",
                                "pointer_type",
                                "slice_type",
                                "map_type",
                                "qualified_type",
                            ]:
                                type_name = get_node_text_fn(subchild)
                        for name in param_names:
                            if type_name:
                                param_types[name] = type_name
                    elif param_child.type == "variadic_parameter_declaration":
                        param_name = None
                        type_name = None
                        for subchild in param_child.children:
                            if subchild.type == "identifier":
                                param_name = get_node_text_fn(subchild)
                            elif subchild.type in [
                                "type_identifier",
                                "pointer_type",
                                "slice_type",
                            ]:
                                type_name = get_node_text_fn(subchild)
                        if param_name and type_name:
                            param_types[param_name] = type_name
        return param_types

    for child in node.children:
        param_types.update(extract_go_parameter_types(child, function_start_line, get_node_text_fn))

    return param_types


def extract_go_assignment_type(node: Any, get_node_text_fn) -> Tuple[Optional[str], Optional[str]]:
    """Extract (var_name, type_name) from Go variable declaration or short var declaration."""
    var_name = None
    type_name = None

    if node.type == "var_spec":
        for child in node.children:
            if child.type == "identifier" and var_name is None:
                var_name = get_node_text_fn(child)
            elif child.type in [
                "type_identifier",
                "pointer_type",
                "slice_type",
                "map_type",
                "qualified_type",
            ]:
                type_name = get_node_text_fn(child)
        # If no explicit type, try to infer from composite literal
        if var_name and not type_name:
            for child in node.children:
                if child.type == "expression_list":
                    for expr in child.children:
                        if expr.type == "composite_literal":
                            for lit_child in expr.children:
                                if lit_child.type == "type_identifier":
                                    type_name = get_node_text_fn(lit_child)
                                    break

    elif node.type == "short_var_declaration":
        # x := SomeType{} or x := NewSomeType()
        left = None
        right = None
        for child in node.children:
            if child.type == "expression_list":
                if left is None:
                    left = child
                else:
                    right = child
        if left and right:
            # Get first variable name
            for child in left.children:
                if child.type == "identifier":
                    var_name = get_node_text_fn(child)
                    break
            # Try to get type from right side
            for child in right.children:
                if child.type == "composite_literal":
                    for lit_child in child.children:
                        if lit_child.type == "type_identifier":
                            type_name = get_node_text_fn(lit_child)
                            break
                elif child.type == "call_expression":
                    # x := NewFoo() -> infer Foo if function starts with "New"
                    if child.children:
                        func_node = child.children[0]
                        func_name = get_node_text_fn(func_node)
                        if func_name and func_name.startswith("New") and len(func_name) > 3:
                            type_name = func_name[3:]

    return var_name, type_name


def get_go_parameter_names(node: Any, function_start_line: int, get_node_text_fn) -> List[str]:
    """Extract Go parameter names in order."""
    param_names = []

    if (
        node.type in ["function_declaration", "method_declaration"]
        and node.start_point[0] == function_start_line
    ):
        # For methods, skip the receiver parameter list (first one)
        param_lists = [c for c in node.children if c.type == "parameter_list"]
        # Last parameter_list is the actual params (first is receiver for methods)
        target_list = param_lists[-1] if param_lists else None

        if target_list:
            for param_child in target_list.children:
                if param_child.type == "parameter_declaration":
                    for subchild in param_child.children:
                        if subchild.type == "identifier":
                            param_names.append(get_node_text_fn(subchild))
                elif param_child.type == "variadic_parameter_declaration":
                    for subchild in param_child.children:
                        if subchild.type == "identifier":
                            param_names.append(get_node_text_fn(subchild))
        if param_names:
            return param_names

    for child in node.children:
        result = get_go_parameter_names(child, function_start_line, get_node_text_fn)
        if result:
            return result

    return param_names
