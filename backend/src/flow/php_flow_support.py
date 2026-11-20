"""PHP-specific flow analysis support methods.

These helpers feed the language-agnostic flow analyzer with the local type
information it needs to resolve method calls such as ``$obj->method()`` to a
concrete component. Variable names are normalised without the leading ``$`` and
class types are stripped of any namespace prefix so they match the call names
produced by :class:`~codekb.parsers.php_parser.PhpParser`.
"""

from typing import Dict, List, Optional, Tuple, Any


_FUNCTION_NODE_TYPES = ("function_definition", "method_declaration")
_PARAM_NODE_TYPES = ("simple_parameter", "property_promotion_parameter")


def _simple_type_name(text: Optional[str]) -> Optional[str]:
    """Strip a PHP namespace prefix (``Foo\\Bar`` -> ``Bar``)."""
    if not text or text == "unknown":
        return None
    return text.split("\\")[-1]


def _type_name_from_param(param_node: Any, get_node_text_fn) -> Optional[str]:
    """Extract a class type name from a parameter node, if it has one."""
    for child in param_node.children:
        if child.type == "named_type":
            for sub in child.children:
                if sub.type in ("name", "qualified_name"):
                    return _simple_type_name(get_node_text_fn(sub))
            return _simple_type_name(get_node_text_fn(child))
        if child.type == "optional_type":
            for sub in child.children:
                if sub.type == "named_type":
                    for inner in sub.children:
                        if inner.type in ("name", "qualified_name"):
                            return _simple_type_name(get_node_text_fn(inner))
    return None


def _variable_name_from_param(param_node: Any, get_node_text_fn) -> Optional[str]:
    """Extract the plain (no ``$``) variable name from a parameter node."""
    for child in param_node.children:
        if child.type == "variable_name":
            for sub in child.children:
                if sub.type == "name":
                    return get_node_text_fn(sub)
            text = get_node_text_fn(child)
            return text.lstrip("$") if text and text != "unknown" else None
    return None


def extract_php_parameter_types(
    node: Any, function_start_line: int, get_node_text_fn
) -> Dict[str, str]:
    """Extract parameter type annotations from a PHP function/method."""
    param_types: Dict[str, str] = {}

    if node.type in _FUNCTION_NODE_TYPES and node.start_point[0] == function_start_line:
        for child in node.children:
            if child.type != "formal_parameters":
                continue
            for param_child in child.children:
                if param_child.type in _PARAM_NODE_TYPES:
                    var_name = _variable_name_from_param(param_child, get_node_text_fn)
                    type_name = _type_name_from_param(param_child, get_node_text_fn)
                    if var_name and type_name:
                        param_types[var_name] = type_name
        return param_types

    for child in node.children:
        param_types.update(
            extract_php_parameter_types(child, function_start_line, get_node_text_fn)
        )

    return param_types


def extract_php_assignment_type(node: Any, get_node_text_fn) -> Tuple[Optional[str], Optional[str]]:
    """Extract ``(var_name, type_name)`` from a PHP ``$x = new Foo()`` assignment."""
    var_name: Optional[str] = None
    type_name: Optional[str] = None

    for child in node.children:
        if child.type == "variable_name":
            for sub in child.children:
                if sub.type == "name":
                    var_name = get_node_text_fn(sub)
                    break
            if var_name is None:
                text = get_node_text_fn(child)
                var_name = text.lstrip("$") if text and text != "unknown" else None
        elif child.type == "object_creation_expression":
            for sub in child.children:
                if sub.type in ("name", "qualified_name"):
                    type_name = _simple_type_name(get_node_text_fn(sub))
                    break

    return var_name, type_name


def get_php_parameter_names(node: Any, function_start_line: int, get_node_text_fn) -> List[str]:
    """Extract PHP parameter names (no ``$``) in declaration order."""
    param_names: List[str] = []

    if node.type in _FUNCTION_NODE_TYPES and node.start_point[0] == function_start_line:
        for child in node.children:
            if child.type != "formal_parameters":
                continue
            for param_child in child.children:
                if param_child.type in _PARAM_NODE_TYPES + ("variadic_parameter",):
                    name = _variable_name_from_param(param_child, get_node_text_fn)
                    if name:
                        param_names.append(name)
        if param_names:
            return param_names

    for child in node.children:
        result = get_php_parameter_names(child, function_start_line, get_node_text_fn)
        if result:
            return result

    return param_names
