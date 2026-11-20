"""Kotlin-specific flow analysis support methods."""

from typing import Dict, List, Optional, Tuple, Any


def extract_kotlin_parameter_types(
    node: Any, function_start_line: int, get_node_text_fn
) -> Dict[str, str]:
    """Extract parameter type annotations from Kotlin function declaration."""
    param_types = {}

    if node.type == "function_declaration" and node.start_point[0] == function_start_line:
        for child in node.children:
            if child.type == "function_value_parameters":
                for param_child in child.children:
                    if param_child.type == "parameter":
                        param_name = None
                        type_name = None
                        for subchild in param_child.children:
                            if subchild.type == "identifier":
                                param_name = get_node_text_fn(subchild)
                            elif subchild.type == "user_type":
                                type_name = get_node_text_fn(subchild)
                            elif subchild.type == "nullable_type":
                                type_name = get_node_text_fn(subchild)
                        if param_name and type_name:
                            param_types[param_name] = type_name
        return param_types

    for child in node.children:
        param_types.update(
            extract_kotlin_parameter_types(child, function_start_line, get_node_text_fn)
        )

    return param_types


def extract_kotlin_assignment_type(
    node: Any, get_node_text_fn
) -> Tuple[Optional[str], Optional[str]]:
    """Extract (var_name, type_name) from Kotlin property declaration."""
    var_name = None
    type_name = None

    # property_declaration contains variable_declaration with optional type annotation
    for child in node.children:
        if child.type == "variable_declaration":
            for subchild in child.children:
                if subchild.type == "identifier":
                    var_name = get_node_text_fn(subchild)
                elif subchild.type == "user_type":
                    type_name = get_node_text_fn(subchild)
                elif subchild.type == "nullable_type":
                    type_name = get_node_text_fn(subchild)

    # If no explicit type, try to infer from constructor call on the right side
    if var_name and not type_name:
        for child in node.children:
            if child.type == "call_expression":
                if child.children:
                    func_node = child.children[0]
                    if func_node.type == "identifier":
                        # val user = User(...) -> type is User
                        type_name = get_node_text_fn(func_node)

    return var_name, type_name


def get_kotlin_parameter_names(node: Any, function_start_line: int, get_node_text_fn) -> List[str]:
    """Extract Kotlin parameter names in order."""
    param_names = []

    if node.type == "function_declaration" and node.start_point[0] == function_start_line:
        for child in node.children:
            if child.type == "function_value_parameters":
                for param_child in child.children:
                    if param_child.type == "parameter":
                        for subchild in param_child.children:
                            if subchild.type == "identifier":
                                param_names.append(get_node_text_fn(subchild))
                                break
        if param_names:
            return param_names

    for child in node.children:
        result = get_kotlin_parameter_names(child, function_start_line, get_node_text_fn)
        if result:
            return result

    return param_names
