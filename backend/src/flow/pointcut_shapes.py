"""The AspectJ pointcut shapes annotation-handler adapters accept.

This is an allowlist, not a parser of the AspectJ language. ``parse_pointcut``
splits an expression into its top-level ``||`` alternatives and returns them as
``Leaf`` values when every alternative is one of these shapes:

- ``@annotation(N)``          -> ``Leaf(ANNOTATION, N)``
- ``@within(N)``              -> ``Leaf(WITHIN, N)``
- ``execution(@N * *(..))``   -> ``Leaf(EXECUTION_ANNOTATED, N)``
- ``name()``                  -> ``Leaf(REFERENCE, name)``, a named pointcut

``N`` is a simple or dotted Java name, returned as written; what a simple name
means (a bound advice parameter, a type name) is the caller's decision. Only
the whitespace AspectJ skips (space, tab, CR, LF) may appear around tokens.

Anything else is refused (None): ``!``, ``&&``, parentheses around
alternatives, the words ``and`` / ``or`` / ``not`` (Spring rewrites them into
operators), other designators, wildcards, and any other character.

To admit a new shape (say ``execution`` with a type/method glob), add one
alternative to ``_LEAF`` and one kind constant; an adapter that meets a kind
it does not handle must refuse the whole expression.
"""

import re
from typing import NamedTuple, Optional, Tuple

ANNOTATION = "@annotation"
WITHIN = "@within"
EXECUTION_ANNOTATED = "execution(@)"
REFERENCE = "reference"


class Leaf(NamedTuple):
    kind: str
    name: str


_WS = r"[ \t\r\n]*"
_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_NAME = rf"{_IDENT}(?:\.{_IDENT})*"
_LEAF = re.compile(
    rf"@annotation{_WS}\({_WS}(?P<annotation>{_NAME}){_WS}\)"
    rf"|@within{_WS}\({_WS}(?P<within>{_NAME}){_WS}\)"
    rf"|execution{_WS}\({_WS}@(?P<execution>{_NAME})[ \t\r\n]+\*[ \t\r\n]+\*{_WS}"
    rf"\({_WS}\.\.{_WS}\){_WS}\)"
    rf"|(?P<reference>{_IDENT}){_WS}\({_WS}\)"
)
_KINDS = {
    "annotation": ANNOTATION,
    "within": WITHIN,
    "execution": EXECUTION_ANNOTATED,
    "reference": REFERENCE,
}
_SPACE = re.compile(_WS)
# A standalone and/or/not: Spring turns " and " / " or " / " not " into
# operators before AspectJ reads the expression, so any such word is refused.
_WORD_OPERATOR = re.compile(r"(?<![A-Za-z0-9_$.])(?:and|or|not)(?![A-Za-z0-9_$.])")
# Designators and keywords that read like a named-pointcut reference.
_NOT_A_REFERENCE = frozenset(
    "adviceexecution args bean call cflow cflowbelow execution get handler if initialization"
    " preinitialization set staticinitialization target this within withincode".split()
)


def parse_pointcut(expression: str) -> Optional[Tuple[Leaf, ...]]:
    """The ``||`` alternatives of ``expression`` as leaves, or None when refused."""
    if _WORD_OPERATOR.search(expression):
        return None
    leaves = []
    pos = _SPACE.match(expression).end()
    while True:
        match = _LEAF.match(expression, pos)
        if match is None:
            return None
        group = match.lastgroup
        name = match.group(group)
        if group == "reference" and name in _NOT_A_REFERENCE:
            return None
        leaves.append(Leaf(_KINDS[group], name))
        pos = _SPACE.match(expression, match.end()).end()
        if pos == len(expression):
            return tuple(leaves)
        if not expression.startswith("||", pos):
            return None
        pos = _SPACE.match(expression, pos + 2).end()
