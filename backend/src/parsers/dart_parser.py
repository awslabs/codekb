"""
Dart-specific parser implementation.

Two properties of Dart shape this file.

The grammar is flat where other languages' are nested: a function is a
``function_signature`` followed by a SIBLING ``function_body``, not one node
wrapping both, and a top-level variable is a bare run of modifier, type and
identifier-list siblings. Extraction therefore walks sibling runs and pairs them
up, stitching a line range from signature start to body end.

Dart also puts several distinct members under one bare name in one file: a getter
beside its setter, an unnamed constructor beside its own class, ``build`` on every
widget. The catalog resolves an identity collision last-write-wins, so every
identity here is qualified enough to keep those apart. Only accessors take a kind
suffix (``.get``/``.set``); a constructor takes none, because a call site writes
the bare ``Class.name`` and a suffixed constructor would be unreachable.
"""

import logging
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .base_parser import BaseLanguageParser
from ..core.models import (
    Component,
    ComponentAttributes,
    ComponentType,
    Parameter,
    ParseDiagnostic,
    ParseDiagnosticKind,
    Visibility,
)

logger = logging.getLogger(__name__)

# A cast whose asserted type reduces to one of these says nothing a lookup can
# use, so `(x as dynamic).find()` yields no call name rather than a bare `find`.
# Same set as dart_flow_support._NO_INFORMATION_TYPES; a test pins them together.
_NO_INFORMATION_CAST_TYPES = frozenset(
    {"dynamic", "var", "Object", "void", "Never", "Function", "unknown"}
)

# A constructor is spelled three ways, and none of them carries a kind suffix:
# the identity has to be the name a call site writes, so a named constructor
# keeps its own name and an unnamed one is named `new`.
_CONSTRUCTOR_SIGNATURES = frozenset(
    {
        "constructor_signature",
        "factory_constructor_signature",
        "constant_constructor_signature",
    }
)

# The signature node types that stand for a declaration with a name we extract.
_MEMBER_SIGNATURES = _CONSTRUCTOR_SIGNATURES | frozenset(
    {
        "function_signature",
        "getter_signature",
        "setter_signature",
        "operator_signature",
    }
)

# Class-head modifiers. `sealed` and `final` both mean "not extensible here", so
# both set is_final; `base` and `interface` restrict but do not seal.
_CLASS_MODIFIERS = frozenset({"abstract", "sealed", "final", "base", "interface", "mixin"})

# Nodes that may precede a top-level or field identifier list: the declaration's
# modifiers and its type annotation. Walking back over these finds the start of
# the declaration, since the grammar gives it no wrapper node at top level.
_DECLARATION_PREFIXES = frozenset(
    {
        "const_builtin",
        "final_builtin",
        "late",
        "static",
        "external",
        "covariant",
        "inferred_type",
        "type_identifier",
        "type_arguments",
        "nullable_type",
        "function_type",
        "record_type",
        "void_type",
    }
)

_IDENTIFIER_LISTS = frozenset({"initialized_identifier_list", "static_final_declaration_list"})

# The enclosing name an unnamed extension's members carry, written so it cannot
# be mistaken for a declared Dart name and cannot collide with a real class of
# the extended type's name in the same file.
_UNNAMED_EXTENSION_PREFIX = "extension$"

# Type-annotation nodes that may spell the type an extension extends, in the run
# of siblings between the `on` keyword and the extension body.
_TYPE_NODES = frozenset(
    {
        "type_identifier",
        "type_arguments",
        "nullable_type",
        "void_type",
        "function_type",
        "record_type",
        "inferred_type",
    }
)

# A recovered tree can offer a keyword where a name belongs: `class const Foo`
# parses with `const` as the class name and the real name stranded in an ERROR
# node, which would publish a component called `const` and lose `Foo`. The three
# groups below are Dart's own keyword table, kept apart so each is checkable.
_DART_RESERVED_WORDS = frozenset(
    {
        "assert",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "default",
        "do",
        "else",
        "enum",
        "extends",
        "false",
        "final",
        "finally",
        "for",
        "if",
        "in",
        "is",
        "new",
        "null",
        "rethrow",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "var",
        "void",
        "while",
        "with",
    }
)

# Barred from a declared type, type-parameter, extension, mixin or typedef
# name, and legal as an ordinary member, function or variable name.
_DART_BUILT_IN_IDENTIFIERS = frozenset(
    {
        "abstract",
        "as",
        "covariant",
        "deferred",
        "dynamic",
        "export",
        "extension",
        "external",
        "factory",
        "Function",
        "get",
        "implements",
        "import",
        "interface",
        "late",
        "library",
        "mixin",
        "operator",
        "part",
        "required",
        "set",
        "static",
        "typedef",
    }
)

# Reserved only in certain positions: `await`/`yield` inside an async or
# generator body, and the Dart 3 class modifiers in a declaration head.
_DART_LIMITED_RESERVED_WORDS = frozenset({"await", "yield", "base", "sealed", "when"})

# Dart's contextual keywords (`async`, `of`, `on`, `show`...) are in neither
# set: they are valid identifiers outside their own clause, and `of` names a
# static method on half of Flutter. The two projections below are load-bearing:
# a reserved word is never a declared name, but a built-in identifier is barred
# only from a declared TYPE name — `get` and `export` are ordinary Dart methods.
_UNTRUSTWORTHY_NAMES_ANYWHERE = _DART_RESERVED_WORDS
_UNTRUSTWORTHY_TYPE_NAMES = (
    _DART_RESERVED_WORDS | _DART_BUILT_IN_IDENTIFIERS | _DART_LIMITED_RESERVED_WORDS
)

# The member name an unnamed constructor is given. It is `new` — a reserved word
# — because that is what a call site writes, so the guard tests the name
# the developer DECLARED and never this synthesised one.
_SYNTHESISED_CONSTRUCTOR_NAME = "new"


class DartParser(BaseLanguageParser):
    """Dart-specific parser implementation."""

    def __init__(self) -> None:
        """Start with no recorded refusals.

        Reset per ``extract_components`` call and drained by the wrapper after it
        returns, so one file's refusals cannot be attributed to the next.
        """
        self._untrusted_names: List[ParseDiagnostic] = []

    def drain_parse_diagnostics(self) -> List[ParseDiagnostic]:
        """Return the refusals recorded for the file just extracted, and clear them."""
        drained = self._untrusted_names
        self._untrusted_names = []
        return drained

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in Dart.

        Dart has no single call-expression node: ``min(1, 2)`` parses as an
        ``identifier`` followed by a sibling ``selector`` holding the argument list. The
        argument-bearing ``selector`` is therefore the only node that reliably marks a
        call, and ``extract_call_name`` reads the callee by walking back from it.
        """
        return ["selector", "new_expression", "cascade_section"]

    def get_function_node_types(self) -> List[str]:
        """
        Return AST node types that represent function definitions in Dart.

        The flow analyzer uses these to stop attributing a nested function's
        calls to its enclosing component, so every form that can introduce a new
        body is listed, including the local and anonymous ones.
        """
        return [
            "function_signature",
            "getter_signature",
            "setter_signature",
            "operator_signature",
            "constructor_signature",
            "factory_constructor_signature",
            "constant_constructor_signature",
            "method_signature",
            "local_function_declaration",
            "function_expression",
            "lambda_expression",
        ]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract the callee name from a Dart call node, as written at the call site.

        The shared walker hands us ``children[0]``, which for a selector is the
        ``argument_part`` — the callee is to the LEFT of the selector, hence the climb to
        the parent and the walk back over previous siblings. At most a receiver and a
        method name are returned (``obj.addAll``), the shape the resolver splits on.
        """
        try:
            node_type = function_node.type

            # new Foo() — the shared walker passes the class name node directly.
            if node_type in ("type_identifier", "identifier"):
                return self._node_text(function_node)

            # A `.name` or `?.name` selector alone is a property read, not a call. The
            # invocation is reported by the argument selector that follows, so returning
            # None here avoids counting the same call twice.
            if node_type in (
                "unconditional_assignable_selector",
                "conditional_assignable_selector",
            ):
                return None

            # r..save() — the walker hands us the anonymous `..` token, so the
            # section is found through its parent. Every call written in a
            # cascade arrives here and nowhere else.
            parent = function_node.parent
            if parent is not None and parent.type == "cascade_section":
                return self._cascaded_callee(parent)

            if node_type in ("argument_part", "arguments"):
                holder = function_node.parent
                while holder is not None and holder.type not in ("selector", "cascade_section"):
                    holder = holder.parent
                if holder is None:
                    return None
                if holder.type == "cascade_section":
                    return self._cascaded_callee(holder)
                return self._callee_before(holder)
        except Exception:
            return None
        return None

    def _cascaded_callee(self, section: Any) -> Optional[str]:
        """Read the callee of one cascade section, attributed to the cascade target.

        ``r..save()..clear()`` is two sections over one target, written once to the left
        of the first section, so the walk back skips earlier sections to reach it. The
        method name is the section's own ``cascade_selector`` unless it writes further
        selectors (``..a.b()``), where the invoked member belongs to the property rather
        than the target. A section with no arguments invokes nothing.
        """
        method = None
        nested = None
        invoked = False
        for child in section.children:
            if child.type == "cascade_selector":
                method = self._first_child_text(child, "identifier")
            elif child.type in (
                "unconditional_assignable_selector",
                "conditional_assignable_selector",
            ):
                nested = self._first_child_text(child, "identifier")
            elif child.type == "argument_part":
                invoked = True
                break
        if not invoked:
            return None
        if nested:
            return nested
        if not method:
            return None

        target = section.prev_sibling
        while target is not None and target.type == "cascade_section":
            target = target.prev_sibling
        return self._receiver_qualified(target, method)

    def _callee_before(self, selector: Any) -> Optional[str]:
        """Read the callee written immediately before an argument selector."""
        previous = selector.prev_sibling
        if previous is None:
            return None

        # obj.addAll(x) / r?.find(): the method name sits in a `.name` or `?.name`
        # selector with its receiver one sibling further left. The conditional form
        # reads the same way, so `r?.find()` and `r.find()` each produce one edge.
        if previous.type == "selector":
            method = None
            for child in previous.children:
                if child.type in (
                    "unconditional_assignable_selector",
                    "conditional_assignable_selector",
                ):
                    method = self._first_child_text(child, "identifier")
            if not method:
                return None
            return self._receiver_qualified(previous.prev_sibling, method)

        # min(1, 2): a bare identifier directly before the arguments.
        if previous.type in ("identifier", "this"):
            return self._node_text(previous)

        return None

    def _receiver_qualified(self, receiver: Optional[Any], method: str) -> Optional[str]:
        """Qualify a method name with its receiver, as the call resolver splits it.

        A plain ``identifier`` or ``this`` receiver reads as written, which is what the
        type machinery needs: the resolver looks it up in the local variable types and
        asks for ``<type>.<method>``. A cast receiver must not fall through to a bare
        name — ``(x as Repo).find()`` would yield ``find``, a key every component
        registers, and resolve to an unrelated member.
        """
        if receiver is None:
            return method
        if receiver.type in ("identifier", "this"):
            text = self._node_text(receiver)
            return f"{text}.{method}" if text else method

        cast_type = self._parenthesized_cast_type(receiver)
        if cast_type is not None:
            return f"{cast_type}.{method}" if cast_type else None

        return method

    def _parenthesized_cast_type(self, receiver: Any) -> Optional[str]:
        """Read the asserted type of a ``(x as Repo)`` receiver.

        Three distinct answers: a reduced type name qualifies the call, an empty string
        suppresses it, and None leaves the existing bare-name behaviour alone.
        """
        if receiver.type != "parenthesized_expression":
            return None

        cast = None
        for child in receiver.children:
            if child.type == "type_cast_expression":
                cast = child
                break
        if cast is None:
            return None

        written = None
        for child in cast.children:
            if child.type != "type_cast":
                continue
            parts: List[str] = []
            seen_operator = False
            for part in child.children:
                if part.type == "as_operator":
                    seen_operator = True
                    continue
                if seen_operator:
                    text = self._node_text(part)
                    if not text or text == "unknown":
                        parts = []
                        break
                    parts.append(text)
            written = "".join(parts)
            break

        return self._simple_cast_type_name(written) or ""

    @staticmethod
    def _simple_cast_type_name(text: Optional[str]) -> Optional[str]:
        """Reduce a written cast type to the simple class name lookups are keyed on.

        ``Repo?`` -> ``Repo``, ``List<Repo>`` -> ``List``, ``prefix.Repo`` -> ``Repo``.
        Refuses a name carrying no information, and a lone upper-case letter (a generic).
        """
        if not text:
            return None
        name = text.strip().split("<")[0].strip()
        while name.endswith("?"):
            name = name[:-1].strip()
        name = name.split(".")[-1].strip()
        if not name or name in _NO_INFORMATION_CAST_TYPES:
            return None
        if len(name) <= 2 and name[0].isupper() and (len(name) == 1 or name[1].isdigit()):
            return None
        if not (name[0].isalpha() or name[0] in "_$"):
            return None
        if not all(char.isalnum() or char in "_$" for char in name):
            return None
        return name

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from a Dart syntax tree.

        Encoding is neither normalised nor diagnosed: the wrapper has already parsed,
        and the grammar tolerates a byte-order mark and invalid bytes without shifting
        a line. Every text read here comes from node bytes.
        """
        components: List[Component] = []
        self._untrusted_names = []
        # Top-level id prefix for this file. Under V1 this is the file stem
        # (unchanged); under V2 it is a path-hashed prefix so same-named Dart
        # files in different directories don't collide. file_prefix's V1 branch
        # returns file_path.stem, identical to the former _file_stem(file_path).
        stem = self.file_prefix(file_path)

        children = list(root_node.children)
        for index, node in enumerate(children):
            try:
                if node.type == "class_definition":
                    # `mixin class Foo` and `interface class Foo` arrive here too: the grammar
                    # spells both as a class_definition with a modifier child.
                    components.extend(self._extract_class(node, children, index, file_path, stem))
                elif node.type == "mixin_declaration":
                    components.extend(self._extract_mixin(node, children, index, file_path, stem))
                elif node.type == "extension_declaration":
                    components.extend(
                        self._extract_extension(node, children, index, file_path, stem)
                    )
                elif node.type == "extension_type_declaration":
                    components.extend(
                        self._extract_extension_type(node, children, index, file_path, stem)
                    )
                elif node.type == "type_alias":
                    component = self._extract_type_alias(node, children, index, file_path, stem)
                    if component:
                        components.append(component)
                elif node.type == "enum_declaration":
                    components.extend(self._extract_enum(node, children, index, file_path, stem))
                elif node.type in ("function_signature", "getter_signature", "setter_signature"):
                    component = self._extract_callable(
                        node,
                        node,
                        self._body_after(children, index),
                        children,
                        index,
                        file_path,
                        owner_id=None,
                        owner_name=None,
                        stem=stem,
                    )
                    if component:
                        components.append(component)
                elif node.type in _IDENTIFIER_LISTS:
                    components.extend(
                        self._extract_variables(
                            node, children, index, file_path, stem, owner_id=None
                        )
                    )
            except Exception as e:
                # One malformed declaration must not cost the rest of the file,
                # and one file must never fail a run.
                logger.debug(f"Skipped a Dart declaration in {file_path}: {e}")

        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """
        Extract parameters from a Dart function-like node.

        Tolerant about what it is handed: the flow analyzer passes whichever node
        it is holding, which may be the ``method_signature`` wrapper rather than
        the inner signature that actually owns the parameter list.
        """
        parameter_list = self._find_parameter_list(function_node)
        if parameter_list is None:
            return []
        return self._parameters_from_list(parameter_list)

    # ── declaration extraction ────────────────────────────────────────────────

    def _extract_class(
        self,
        node: Any,
        siblings: List[Any],
        index: int,
        file_path: Path,
        stem: str,
    ) -> List[Component]:
        """Extract a class and every member of its body."""
        name = self._declared_name(node, file_path, type_name=True)
        if not name:
            # A class whose name could not be trusted takes its members with
            # it: their identities are qualified by that name, so emitting them
            # would file real behaviour under a name the developer never wrote.
            return []

        class_id = f"{stem}.{name}"
        modifiers = {child.type for child in node.children} & _CLASS_MODIFIERS
        body = self._first_child(node, "class_body")

        attributes = ComponentAttributes(
            signature=self._head_signature(node, "class_body"),
            visibility=self._visibility(name),
            documentation=self._documentation(siblings, index),
            line_count=node.end_point[0] - node.start_point[0] + 1,
            is_abstract="abstract" in modifiers,
            is_final=bool(modifiers & {"final", "sealed"}),
        )

        components = [
            self._component(class_id, ComponentType.CLASS, node, None, file_path, attributes, None)
        ]
        if body is not None:
            components.extend(self._extract_members(body, class_id, name, file_path, stem))
        return components

    def _extract_mixin(
        self,
        node: Any,
        siblings: List[Any],
        index: int,
        file_path: Path,
        stem: str,
    ) -> List[Component]:
        """Extract a ``mixin Foo`` declaration and every member of its body.

        A mixin is a named, type-shaped declaration, so it maps to ``class`` rather than
        a new kind. Its members take the same path a class's do, under the mixin's name.
        """
        name = self._declared_name(node, file_path, type_name=True)
        if not name:
            return []

        mixin_id = f"{stem}.{name}"
        modifiers = {child.type for child in node.children} & _CLASS_MODIFIERS
        body = self._first_child(node, "class_body")

        attributes = ComponentAttributes(
            signature=self._head_signature(node, "class_body"),
            visibility=self._visibility(name),
            documentation=self._documentation(siblings, index),
            line_count=node.end_point[0] - node.start_point[0] + 1,
            is_final=bool(modifiers & {"final", "sealed"}),
        )

        components = [
            self._component(mixin_id, ComponentType.CLASS, node, None, file_path, attributes, None)
        ]
        if body is not None:
            components.extend(self._extract_members(body, mixin_id, name, file_path, stem))
        return components

    def _extract_extension(
        self,
        node: Any,
        siblings: List[Any],
        index: int,
        file_path: Path,
        stem: str,
    ) -> List[Component]:
        """Extract an extension declaration, named or unnamed.

        A named extension carries the extended type in its SIGNATURE and never in its
        identity: a ``String`` extension's ``shout`` is not a method on ``String``. An
        unnamed one yields no component for itself, but its members are extracted.
        """
        body = self._first_child(node, "extension_body")
        if body is None:
            return []

        name_node = self._first_child(node, "identifier")
        if name_node is not None:
            # A name that cannot be trusted is NOT the same as no declared name: falling
            # through to the unnamed path would file this extension's members under its
            # extended type, which the developer did not ask for either.
            name = self._trusted_name(name_node, file_path, type_name=True)
            if name is None:
                return []
            extension_id = f"{stem}.{name}"
            attributes = ComponentAttributes(
                signature=self._head_signature(node, "extension_body"),
                visibility=self._visibility(name),
                documentation=self._documentation(siblings, index),
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )
            components = [
                self._component(
                    extension_id, ComponentType.CLASS, node, None, file_path, attributes, None
                )
            ]
            components.extend(self._extract_members(body, extension_id, name, file_path, stem))
            return components

        marker = self._unnamed_extension_name(node)
        if marker is None:
            # No readable `on` clause, which valid Dart always has. Recorded as a
            # gap rather than guessed at, per the grammar-acceptance rule.
            logger.debug(f"Unnamed Dart extension in {file_path} has no readable `on` clause")
            return []
        return self._extract_members(
            body, f"{stem}.{marker}", marker, file_path, stem, attach_to_owner=False
        )

    def _extract_extension_type(
        self,
        node: Any,
        siblings: List[Any],
        index: int,
        file_path: Path,
        stem: str,
    ) -> List[Component]:
        """Extract an ``extension type Name(T value)`` declaration and its members.

        An extension type is not an extension: it is a wrapper type with its own
        representation and declared name, and the grammar gives it its own node type.
        Its members are attributed to its own name, as a class's are.
        """
        name = self._declared_name(node, file_path, type_name=True)
        if not name:
            return []

        type_id = f"{stem}.{name}"
        body = self._first_child(node, "class_body")

        attributes = ComponentAttributes(
            signature=self._head_signature(node, "class_body"),
            visibility=self._visibility(name),
            documentation=self._documentation(siblings, index),
            line_count=node.end_point[0] - node.start_point[0] + 1,
        )

        components = [
            self._component(type_id, ComponentType.CLASS, node, None, file_path, attributes, None)
        ]
        if body is not None:
            components.extend(self._extract_members(body, type_id, name, file_path, stem))
        return components

    def _extract_type_alias(
        self,
        node: Any,
        siblings: List[Any],
        index: int,
        file_path: Path,
        stem: str,
    ) -> Optional[Component]:
        """Extract a ``typedef`` in either of Dart's two forms.

        A typedef names a type a developer searches for, so it maps to ``class``. The
        signature records whatever it aliases, which is not always a function type.
        """
        name_node, alias = self._type_alias_parts(node)
        if name_node is None:
            return None
        name = self._trusted_name(name_node, file_path, "type identifier", type_name=True)
        if not name:
            return None

        attributes = ComponentAttributes(
            signature=alias,
            visibility=self._visibility(name),
            documentation=self._documentation(siblings, index),
            line_count=node.end_point[0] - node.start_point[0] + 1,
        )
        return self._component(
            f"{stem}.{name}", ComponentType.CLASS, node, None, file_path, attributes, None
        )

    def _type_alias_parts(self, node: Any) -> Tuple[Optional[Any], Optional[str]]:
        """Return (declared-name node, aliased text) for a ``type_alias`` node.

        The name is a node, not text, so the caller can check trust before it becomes an
        identity. The two forms are told apart by the ``=`` token: ``typedef N<T> = X;``
        puts the name left of it, the legacy ``typedef R N(params);`` left of the params.
        """
        children = list(node.children)
        equals = next(
            (position for position, child in enumerate(children) if child.type == "="), None
        )

        if equals is not None:
            name_node = next(
                (child for child in children[:equals] if child.type == "type_identifier"),
                None,
            )
            alias = "".join(
                self._node_text(child) for child in children[equals + 1 :] if child.type != ";"
            )
            return name_node, alias or None

        parameters = next(
            (
                position
                for position, child in enumerate(children)
                if child.type == "formal_parameter_list"
            ),
            None,
        )
        if parameters is None:
            return None, None
        name_node = next(
            (child for child in reversed(children[:parameters]) if child.type == "type_identifier"),
            None,
        )
        written = self._node_text(node).strip()
        if written.startswith("typedef"):
            written = written[len("typedef") :].strip()
        return name_node, written.rstrip(";").strip() or None

    def _unnamed_extension_name(self, node: Any) -> Optional[str]:
        """Return ``extension$<ExtendedType>``, the name an unnamed extension's members carry.

        The extended type is the run of type siblings between ``on`` and the body.
        Whitespace drops and a dotted prefix collapses to ``$``, keeping it one segment.
        """
        parts: List[str] = []
        seen_on = False
        for child in node.children:
            if child.type == "on":
                seen_on = True
                continue
            if not seen_on:
                continue
            if child.type == "extension_body":
                break
            if child.type in _TYPE_NODES:
                parts.append(self._node_text(child))

        written = "".join("".join(part.split()) for part in parts)
        if not written:
            return None
        return _UNNAMED_EXTENSION_PREFIX + written.replace(".", "$")

    def _extract_enum(
        self,
        node: Any,
        siblings: List[Any],
        index: int,
        file_path: Path,
        stem: str,
    ) -> List[Component]:
        """
        Extract an enum, its values, and (for an enhanced enum) its members.

        An enum value becomes a CONSTANT parented to the enum rather than a new
        component kind, because the kind set is shared with twelve other
        languages, and this change does not widen it.
        """
        name = self._declared_name(node, file_path, type_name=True)
        if not name:
            return []

        enum_id = f"{stem}.{name}"
        body = self._first_child(node, "enum_body")

        attributes = ComponentAttributes(
            signature=self._head_signature(node, "enum_body"),
            visibility=self._visibility(name),
            documentation=self._documentation(siblings, index),
            line_count=node.end_point[0] - node.start_point[0] + 1,
        )
        components = [
            self._component(enum_id, ComponentType.ENUM, node, None, file_path, attributes, None)
        ]

        if body is None:
            return components

        body_children = list(body.children)
        for position, child in enumerate(body_children):
            if child.type != "enum_constant":
                continue
            value_name = self._declared_name(child, file_path)
            if not value_name:
                continue
            value_attributes = ComponentAttributes(
                signature=self._node_text(child),
                visibility=self._visibility(value_name),
                documentation=self._documentation(body_children, position),
                line_count=child.end_point[0] - child.start_point[0] + 1,
            )
            components.append(
                self._component(
                    f"{enum_id}.{value_name}",
                    ComponentType.CONSTANT,
                    child,
                    None,
                    file_path,
                    value_attributes,
                    enum_id,
                )
            )

        components.extend(self._extract_members(body, enum_id, name, file_path, stem))
        return components

    def _extract_members(
        self,
        body: Any,
        owner_id: str,
        owner_name: str,
        file_path: Path,
        stem: str,
        attach_to_owner: bool = True,
    ) -> List[Component]:
        """Extract the members of a class, enum, mixin, extension or extension-type body.

        Two shapes carry a member: ``method_signature`` with a body as its next sibling,
        and ``declaration`` with none — an abstract method or a ``;``-declared
        constructor. A ``declaration`` with no signature is a field, which is not extracted.
        """
        components: List[Component] = []
        children = list(body.children)
        parent_id = owner_id if attach_to_owner else None

        for index, node in enumerate(children):
            if node.type not in ("method_signature", "declaration"):
                continue
            signature = self._inner_signature(node)
            if signature is None:
                continue
            try:
                component = self._extract_callable(
                    node,
                    signature,
                    self._body_after(children, index) if node.type == "method_signature" else None,
                    children,
                    index,
                    file_path,
                    owner_id=owner_id,
                    owner_name=owner_name,
                    stem=stem,
                    parent_id=parent_id,
                )
            except Exception as e:
                logger.debug(f"Skipped a Dart member in {file_path}: {e}")
                continue
            if component:
                components.append(component)

        return components

    def _extract_callable(
        self,
        container: Any,
        signature: Any,
        body: Optional[Any],
        siblings: List[Any],
        index: int,
        file_path: Path,
        owner_id: Optional[str],
        owner_name: Optional[str],
        stem: str,
        parent_id: Optional[str] = None,
    ) -> Optional[Component]:
        """Extract one function-like declaration: function, method, accessor or constructor.

        Only accessors take an identity suffix; a constructor takes none so a call
        site's ``Class.name`` reaches it. ``owner_id`` qualifies the identity and
        ``parent_id`` is what it hangs under — the same except for an unnamed extension.
        """
        named = self._callable_name(signature, owner_name, file_path)
        if named is None:
            return None
        member_name, suffix, declared_name = named

        parts = [owner_id or stem, member_name]
        if suffix:
            parts.append(suffix)
        component_id = ".".join(parts)

        is_constructor = signature.type in _CONSTRUCTOR_SIGNATURES
        if is_constructor:
            component_type = ComponentType.CONSTRUCTOR
        elif signature.type in ("getter_signature", "setter_signature", "operator_signature"):
            # Getters, setters and operators map to `method` as constructs, not by where
            # they sit, so a top-level accessor is a parentless method rather than a
            # function. Only a plain top-level `function_signature` is a function.
            component_type = ComponentType.METHOD
        elif owner_id is not None:
            component_type = ComponentType.METHOD
        else:
            component_type = ComponentType.FUNCTION

        parameter_list = self._find_parameter_list(signature)
        attributes = ComponentAttributes(
            signature=self._node_text(signature),
            parameters=self._parameters_from_list(parameter_list) if parameter_list else [],
            return_type=self._return_type(signature),
            visibility=self._visibility(declared_name),
            documentation=self._documentation(siblings, index),
            line_count=self._span_end(container, body) - container.start_point[0] + 1,
            accessor_kind=suffix or None,
            is_static=self._has_child(container, "static"),
            is_abstract=body is None and not is_constructor,
            is_final=False,
        )

        return self._component(
            component_id, component_type, container, body, file_path, attributes, parent_id
        )

    def _callable_name(
        self, signature: Any, owner_name: Optional[str], file_path: Path
    ) -> Optional[Tuple[str, Optional[str], str]]:
        """Return (member name, identity suffix, declared name) for a signature.

        The declared name is returned separately because visibility reads what the
        developer wrote while the identity uses the form a call site writes. They differ
        for operators, whose member name is synthesised, and for unnamed constructors,
        whose member name is ``new`` while the written name is the class's. The trust
        check applies to what the developer wrote, which is why that matters here.
        """
        signature_type = signature.type

        if signature_type in _CONSTRUCTOR_SIGNATURES:
            # `A()` yields one identifier; `A.named()` yields two, the second the
            # constructor's own name. A named constructor keeps that name because that is
            # what a call site writes; an unnamed one is `new`, which Dart also accepts.
            # Neither carries a suffix, or the call site could not reach it.
            identifier_nodes = [child for child in signature.children if child.type == "identifier"]
            if len(identifier_nodes) >= 2:
                declared = self._trusted_name(identifier_nodes[-1], file_path)
                return (declared, None, declared) if declared else None
            if owner_name:
                # Already trusted: it is the enclosing declaration's own name.
                declared = owner_name
            elif identifier_nodes:
                trusted = self._trusted_name(identifier_nodes[0], file_path)
                if not trusted:
                    return None
                declared = trusted
            else:
                return None
            return _SYNTHESISED_CONSTRUCTOR_NAME, None, declared

        if signature_type == "getter_signature":
            name = self._declared_name(signature, file_path)
            return (name, "get", name) if name else None

        if signature_type == "setter_signature":
            name = self._declared_name(signature, file_path)
            return (name, "set", name) if name else None

        if signature_type == "operator_signature":
            token = self._operator_token(signature)
            if token is None:
                return None
            parameter_list = self._find_parameter_list(signature)
            arity = len(self._collect_formal_parameters(parameter_list)) if parameter_list else 0
            # `[]` and `[]=` differ by token; unary minus (`operator-:0`) differs
            # from binary minus (`operator-:1`) by arity.
            synthesised = f"operator{token}:{arity}"
            return synthesised, None, synthesised

        if signature_type == "function_signature":
            name = self._declared_name(signature, file_path)
            return (name, None, name) if name else None

        return None

    def _extract_variables(
        self,
        list_node: Any,
        siblings: List[Any],
        index: int,
        file_path: Path,
        stem: str,
        owner_id: Optional[str],
    ) -> List[Component]:
        """Extract the top-level variables or constants of one declaration.

        The grammar gives a top-level variable no wrapper node, so modifiers and the
        declared type sit as siblings left of the identifier list.
        """
        start = index
        is_const = False
        type_parts: List[str] = []
        cursor = index - 1
        while cursor >= 0 and siblings[cursor].type in _DECLARATION_PREFIXES:
            node = siblings[cursor]
            if node.type in ("const_builtin", "final_builtin"):
                is_const = True
            # The same type nodes `_DECLARATION_PREFIXES` walks back over, or a
            # variable written with a function or record type records no type at
            # all while one was plainly written.
            if node.type in _TYPE_NODES:
                type_parts.insert(0, self._node_text(node))
            start = cursor
            cursor -= 1

        # `static_final_declaration_list` is the grammar's shape for a const or
        # final declaration, so it is a constant even without a modifier sibling.
        if list_node.type == "static_final_declaration_list":
            is_const = True
        component_type = ComponentType.CONSTANT if is_const else ComponentType.VARIABLE

        start_node = siblings[start]
        type_hint = "".join(type_parts) or None
        components: List[Component] = []

        for entry in list_node.children:
            if entry.type not in ("initialized_identifier", "static_final_declaration"):
                continue
            name = self._declared_name(entry, file_path)
            if not name:
                continue
            attributes = ComponentAttributes(
                signature=self._node_text(start_node) + " " + self._node_text(list_node)
                if start_node is not list_node
                else self._node_text(list_node),
                return_type=type_hint,
                visibility=self._visibility(name),
                documentation=self._documentation(siblings, start),
                line_count=list_node.end_point[0] - start_node.start_point[0] + 1,
            )
            components.append(
                Component(
                    id=f"{owner_id or stem}.{name}",
                    type=component_type,
                    file_path=file_path,
                    line_number=start_node.start_point[0] + 1,
                    end_line_number=list_node.end_point[0] + 1,
                    language="dart",
                    attributes=attributes,
                    parent_component=owner_id,
                )
            )

        return components

    # ── parameters ────────────────────────────────────────────────────────────

    def _parameters_from_list(self, parameter_list: Any) -> List[Parameter]:
        """Extract parameters with their declared name, type and passing style.

        Dart spells the style in the brackets: nothing is positional-required, ``[...]``
        positional-optional, ``{...}`` named. Named is required only with ``required``.
        """
        parameters: List[Parameter] = []

        for child in parameter_list.children:
            if child.type == "formal_parameter":
                parameters.append(self._parameter(child, "positional-required", True, None))
            elif child.type == "optional_formal_parameters":
                parameters.extend(self._optional_parameters(child))

        return parameters

    def _optional_parameters(self, group: Any) -> List[Parameter]:
        """Extract one `[...]` or `{...}` parameter group."""
        is_named = any(child.type == "{" for child in group.children)
        style = "named" if is_named else "positional-optional"

        parameters: List[Parameter] = []
        pending_required = False
        pending: Optional[Parameter] = None
        default_parts: List[str] = []
        collecting_default = False

        def flush() -> None:
            nonlocal pending, default_parts, collecting_default
            if pending is not None:
                if default_parts:
                    pending.default_value = " ".join(default_parts)
                parameters.append(pending)
            pending = None
            default_parts = []
            collecting_default = False

        for child in group.children:
            if child.type == "required":
                pending_required = True
            elif child.type == "formal_parameter":
                flush()
                # A named parameter is optional unless marked `required`; a
                # positional-optional one is optional by definition.
                pending = self._parameter(child, style, is_named and pending_required, None)
                pending_required = False
            elif child.type in ("=", ":"):
                collecting_default = True
            elif child.type == ",":
                flush()
            elif collecting_default and child.type not in ("{", "}", "[", "]"):
                default_parts.append(self._node_text(child))

        flush()
        return parameters

    def _parameter(
        self, node: Any, style: str, required: bool, default_value: Optional[str]
    ) -> Parameter:
        """Build one Parameter from a formal_parameter node."""
        name = None
        type_parts: List[str] = []
        for child in node.children:
            if child.type == "identifier":
                # The last identifier is the parameter name; an earlier one can
                # belong to the type (`Map<String, int> seed`).
                name = self._node_text(child)
            elif child.type in (
                "type_identifier",
                "type_arguments",
                "nullable_type",
                "void_type",
                "function_type",
                "record_type",
                "inferred_type",
            ):
                type_parts.append(self._node_text(child))
        if name is None:
            # `this.payload` and `super.payload` initialising parameters have no
            # bare identifier child; the written text is the honest record.
            name = self._node_text(node)
        return Parameter(
            name=name,
            type_hint="".join(type_parts) or None,
            default_value=default_value,
            required=required,
            style=style,
        )

    def _collect_formal_parameters(self, parameter_list: Any) -> List[Any]:
        """Return every formal_parameter in a list, including the bracketed groups."""
        found: List[Any] = []
        for child in parameter_list.children:
            if child.type == "formal_parameter":
                found.append(child)
            elif child.type == "optional_formal_parameters":
                found.extend(inner for inner in child.children if inner.type == "formal_parameter")
        return found

    def _find_parameter_list(self, node: Any) -> Optional[Any]:
        """Find the formal_parameter_list owned by a signature, one level deep."""
        if node is None:
            return None
        direct = self._first_child(node, "formal_parameter_list")
        if direct is not None:
            return direct
        for child in node.children:
            found = self._first_child(child, "formal_parameter_list")
            if found is not None:
                return found
        return None

    # ── trusting a declared name ──────────────────────────────────────────────

    def _declared_name(
        self,
        owner: Any,
        file_path: Path,
        child_type: str = "identifier",
        *,
        type_name: bool = False,
    ) -> Optional[str]:
        """Return the name a declaration spells, or None when it cannot be trusted.

        None means do not emit this declaration: either the node is absent, or the name
        cannot be trusted and the refusal has been recorded. ``type_name`` says whether
        this is a declared type name, because Dart bars more words there than it does
        from a member or variable name.
        """
        node = self._first_child(owner, child_type)
        if node is None:
            return None
        return self._trusted_name(node, file_path, child_type, type_name=type_name)

    def _trusted_name(
        self,
        name_node: Any,
        file_path: Path,
        described_as: str = "identifier",
        *,
        type_name: bool = False,
    ) -> Optional[str]:
        """Return the name node's text when it can be trusted, else record and refuse."""
        written = self._node_text(name_node).strip()
        if not written:
            return None

        reason = self._distrust_reason(name_node, written, type_name=type_name)
        if reason is None:
            return written

        self._untrusted_names.append(
            ParseDiagnostic(
                file_path=file_path,
                kind=ParseDiagnosticKind.UNTRUSTED_NAME,
                line=name_node.start_point[0] + 1,
                column=name_node.start_point[1],
                byte_offset=name_node.start_byte,
                detail=f"declaration not named from its {described_as}: {reason}",
            )
        )
        logger.warning(
            f"Dart declaration at {file_path}:{name_node.start_point[0] + 1} was not "
            f"named: {reason}. No component is emitted for it."
        )
        return None

    def _distrust_reason(self, name_node: Any, written: str, *, type_name: bool) -> Optional[str]:
        """Return why a name cannot be trusted, or None when it can.

        Two reasons, both from one cause: the grammar recovered from syntax it could not
        parse, so the tree no longer says what the developer wrote. Either the name
        reads as a Dart keyword that cannot stand in this position, or it sits inside an
        ERROR or MISSING node. The keyword test is EXACT and positional, because Dart
        permits ``constValue``, ``Constants``, ``finalise`` and ``_const`` as real names.
        """
        barred = _UNTRUSTWORTHY_TYPE_NAMES if type_name else _UNTRUSTWORTHY_NAMES_ANYWHERE
        if written in barred:
            return f"it reads as the Dart keyword `{written}`"

        broken = self._broken_ancestor(name_node)
        if broken is not None:
            kind = "MISSING" if broken.is_missing else "ERROR"
            return (
                f"it sits inside a {kind} node the grammar produced while "
                f"recovering (line {broken.start_point[0] + 1})"
            )
        return None

    def _broken_ancestor(self, node: Any) -> Optional[Any]:
        """Return the nearest ERROR or MISSING node spanning this one, including itself.

        The file's own root is deliberately not examined: when a Dart file is damaged
        enough, this grammar replaces the ``program`` root with an ERROR node covering
        the whole file, so testing the root would refuse every declaration in it —
        including names that are exactly what the developer wrote.
        """
        cursor: Optional[Any] = node
        while cursor is not None and cursor.parent is not None:
            if cursor.is_error or cursor.is_missing:
                return cursor
            cursor = cursor.parent
        return None

    # ── small helpers ─────────────────────────────────────────────────────────

    def _file_stem(self, file_path: Path) -> str:
        """
        Return the file stem with only the final `.dart` extension removed.

        `Path.stem` drops only the last suffix already, which is the behaviour
        this needs: `my.model.dart` yields `my.model`, dots kept verbatim.
        """
        return file_path.stem

    def _visibility(self, declared_name: Optional[str]) -> Visibility:
        """Dart encodes privacy in the name and nowhere else."""
        if declared_name and declared_name.startswith("_"):
            return Visibility.PRIVATE
        return Visibility.PUBLIC

    def _component(
        self,
        component_id: str,
        component_type: ComponentType,
        container: Any,
        body: Optional[Any],
        file_path: Path,
        attributes: ComponentAttributes,
        parent_component: Optional[str],
    ) -> Component:
        """
        Build a component whose line range spans signature and body.

        The shared `_create_component` reads both line numbers off one node, but
        a Dart declaration's body is a *sibling* of its signature, so the end
        line has to come from the body when there is one.
        """
        component = self._create_component(
            component_id,
            component_type,
            container,
            file_path,
            "dart",
            attributes,
            parent_component,
        )
        component.end_line_number = self._span_end(container, body) + 1
        return component

    def _span_end(self, container: Any, body: Optional[Any]) -> int:
        """Return the zero-based last row of a declaration and its body."""
        container_end = int(container.end_point[0])
        if body is not None:
            return max(container_end, int(body.end_point[0]))
        return container_end

    def _body_after(self, siblings: List[Any], index: int) -> Optional[Any]:
        """Return the function_body that follows a signature, when it has one."""
        following = index + 1
        if following < len(siblings) and siblings[following].type == "function_body":
            return siblings[following]
        return None

    def _inner_signature(self, container: Any) -> Optional[Any]:
        """Return the signature node inside a method_signature or declaration."""
        for child in container.children:
            if child.type in _MEMBER_SIGNATURES:
                return child
        return None

    def _operator_token(self, signature: Any) -> Optional[str]:
        """Return the operator's token, as written, from an operator_signature."""
        children = list(signature.children)
        for position, child in enumerate(children):
            if child.type == "operator" and position + 1 < len(children):
                return self._node_text(children[position + 1]).strip()
        return None

    def _return_type(self, signature: Any) -> Optional[str]:
        """
        Return the declared return type, when the signature annotates one.

        Joined over the same `_TYPE_NODES` the parameter and variable sites read,
        so `Future<void>?` is recorded as written rather than as its first node.
        A method's own `type_parameters` is deliberately not in that set, so
        `T echo<T>(...)` stays `T`.

        Read only from the tree: an unannotated declaration leaves this absent
        rather than guessing `dynamic`.
        """
        parts = [
            self._node_text(child) for child in signature.children if child.type in _TYPE_NODES
        ]
        return "".join(parts) or None

    def _documentation(self, siblings: List[Any], index: int) -> Optional[str]:
        """Join the doc-comment lines immediately preceding a declaration."""
        lines: List[str] = []
        cursor = index - 1
        while cursor >= 0 and siblings[cursor].type == "documentation_comment":
            lines.insert(0, self._node_text(siblings[cursor]))
            cursor -= 1
        return "\n".join(lines) if lines else None

    def _head_signature(self, node: Any, body_type: str) -> Optional[str]:
        """Return a declaration's head text, stopping before its body."""
        parts: List[str] = []
        for child in node.children:
            if child.type == body_type:
                break
            parts.append(self._node_text(child))
        return " ".join(part for part in parts if part) or None

    def _has_child(self, node: Any, child_type: str) -> bool:
        """Report whether a node has a direct child of the given type."""
        return any(child.type == child_type for child in node.children)

    def _first_child(self, node: Any, child_type: str) -> Optional[Any]:
        """Return the first direct child of the given type."""
        for child in node.children:
            if child.type == child_type:
                return child
        return None

    def _first_child_text(self, node: Any, child_type: str) -> Optional[str]:
        """Return the text of the first direct child of the given type."""
        child = self._first_child(node, child_type)
        return self._node_text(child) if child is not None else None

    def _node_text(self, node: Any) -> str:
        """Return a node's text from its own bytes.

        tree-sitter columns are byte offsets, so slicing decoded lines by them misreads
        any line holding a multi-byte character — common in comments and literals.
        """
        try:
            text: str = node.text.decode("utf-8", errors="replace")
            return text
        except Exception:
            return self._get_node_text_simple(node)
