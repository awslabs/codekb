"""
Dart split-library grouping.

Dart lets one library span several files: a parent writes ``part 'state.dart';``
and the part writes ``part of 'models.dart';``. A part file is not independently
compilable, so naming its components after their own file stem both misplaces
them and collides — ``state.dart`` is a name repositories reuse constantly, and
the catalog resolves an identity collision last-write-wins, silently dropping one.

So a part file's components are prefixed with an encoding of their parent's path.
This runs after extraction and before the catalog, because the parser sees one
file at a time and cannot know that a parent claimed it. A relationship that
cannot be used is recorded as an ``UnresolvedPartRecord`` rather than guessed at.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..core.logging_utils import get_logger
from ..core.models import Component

logger = get_logger(__name__)

# The closed set of reasons a part relationship could not be used. A new reason
# is a design change, not a new string.
REASON_NOT_IN_ANALYSED_SET = "not-in-analysed-set"
REASON_CLAIMED_BY_TWO_PARENTS = "claimed-by-two-parents"
REASON_CHAINED_PART = "chained-part"
REASON_ORPHANED_PART = "orphaned-part"

# Keywords that may begin a Dart directive statement. Dart requires every
# directive to precede all declarations, so the first statement that does not
# start with one of these (or with an annotation) ends the directive section.
_DIRECTIVE_KEYWORDS = frozenset({"library", "import", "export", "part"})

# The extension in its canonical spelling. `CodeAnalyzer` selects a Dart file by
# `suffix.lower()`, so `b.DART` is analysed too and `_prefix_for` has to agree
# with that test rather than with a case-sensitive one (R-10).
_DART_EXTENSION = ".dart"

_PART_OF_RE = re.compile(r"^part\s+of\b")
# `part` is matched only as far as its keyword; the URI is read by
# `_scan_string_literal`, because a regex ending at the first quote accepts
# `part 'a' '.dart'` with a garbled target and misses a raw-string URI.
_PART_KEYWORD_RE = re.compile(r"^part\s+(?P<rest>\S.*)$", re.DOTALL)
_LIBRARY_RE = re.compile(r"^library\s+(?P<name>[A-Za-z_$][\w$.]*)\s*$")
_ANNOTATION_RE = re.compile(r"^@\s*[A-Za-z_$][\w$.]*")


@dataclass(frozen=True)
class UnresolvedPartRecord:
    """One part relationship that could not be used, and why.

    Data rather than a log line: the common Flutter shape produces one on nearly
    every repository — ``part 'thing.g.dart'`` beside a generated file the scanner
    excluded — so a reader needs to see what was not grouped.
    """

    part_path: str
    reason: str
    parent_path: Optional[str] = None
    second_parent_path: Optional[str] = None
    declared_parent_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        """Plain-data form, so the record travels in analysis metadata."""
        return {
            "part_path": self.part_path,
            "reason": self.reason,
            "parent_path": self.parent_path,
            "second_parent_path": self.second_parent_path,
            "declared_parent_path": self.declared_parent_path,
        }


class DartLibraryQualifier:
    """Prefixes Dart part-file component IDs with their library's parent path.

    Instantiate once per analysis run with the source directory and the Dart
    files the scanner actually selected; apply to each file's components before
    they are added to the catalog.
    """

    def __init__(self, source_directory: Path, dart_files: Iterable[Path]) -> None:
        """Discover the libraries in the analysed set.

        ``dart_files`` is the set this run selected; membership of it — not presence on
        disk — decides whether a ``part`` directive resolves, so this must not re-walk
        the tree and reach a different answer than the scan did.
        """
        self._source_directory = source_directory.resolve()
        self._records: List[UnresolvedPartRecord] = []

        analysed, ordered = self._resolve_analysed_set(dart_files)
        directives = {path: self._read_directives(path) for path in ordered}
        parents = {path for path, (parts, _, _) in directives.items() if parts}

        claims, claimed_ever = self._collect_claims(ordered, directives, analysed)
        self._drop_chains(claims, parents)
        self._record_orphans(ordered, directives, claimed_ever)

        self._prefix_by_part: Dict[Path, str] = {}
        for part_path, parent_path in claims.items():
            prefix = self._prefix_for(parent_path)
            if prefix:
                self._prefix_by_part[part_path] = prefix
                continue
            # The parent resolved outside the repository root, so there is no
            # repo-relative path to build a prefix from. `not-in-analysed-set` covers
            # both this and the ordinary excluded generated part; `declared_parent_path`
            # is what tells them apart, and it is always populated here.
            logger.debug(
                f"Dart parent {parent_path} of part {self._relative(part_path)} "
                f"resolves outside the analysed root; not qualifying"
            )
            declared = directives.get(part_path, ([], None, None))[1]
            self._records.append(
                UnresolvedPartRecord(
                    part_path=self._relative(part_path),
                    reason=REASON_NOT_IN_ANALYSED_SET,
                    parent_path=self._relative(parent_path),
                    declared_parent_path=declared or self._relative(parent_path),
                )
            )

        self._library_names = {
            path: name for path, (_, _, name) in directives.items() if path in parents and name
        }

        if self.active:
            library_count = len({claims[part] for part in self._prefix_by_part})
            logger.info(
                f"Dart split libraries detected; qualifying part-file component IDs: "
                f"{library_count} library/libraries, "
                f"{len(self._prefix_by_part)} part file(s)"
            )

    @property
    def active(self) -> bool:
        """Whether qualification will change any ID.

        True when at least one recorded library has a resolved part file. The common
        Flutter shape — a parent whose only ``part`` names a generated file the scanner
        excluded — leaves this False: a directive exists, nothing resolves.
        """
        return bool(self._prefix_by_part)

    @property
    def unresolved_records(self) -> List[UnresolvedPartRecord]:
        """Every part relationship that could not be used, in discovery order."""
        return list(self._records)

    @property
    def qualified_part_paths(self) -> List[str]:
        """Repo-relative paths of the part files whose IDs this run prefixes.

        Read-only and deliberately narrow: it reports what discovery already decided and
        takes part in no decision. Added for the space-refresh path, which must know
        which files carry a library prefix in order to reprocess them.
        """
        return [self._relative(part) for part in self._prefix_by_part]

    @property
    def library_names(self) -> Dict[Path, str]:
        """Explicit ``library Name;`` declarations, recorded but never used.

        The prefix always comes from the path, because the declaration is
        optional and modern Dart omits it.
        """
        return dict(self._library_names)

    def qualify(self, components: List[Component]) -> None:
        """Prefix Dart components declared in a resolved part file, in place."""
        if not self.active:
            return
        for component in components:
            if component.language != "dart":
                continue
            prefix = self._prefix_by_part.get(self._resolve(component.file_path))
            if not prefix:
                continue
            component.id = f"{prefix}.{component.id}"
            if component.parent_component:
                component.parent_component = f"{prefix}.{component.parent_component}"

    # ── internals ────────────────────────────────────────────────────────────

    def _resolve_analysed_set(self, dart_files: Iterable[Path]) -> Tuple[Set[Path], List[Path]]:
        """Resolved paths of the analysed Dart files, as a set and in scan order."""
        analysed: Set[Path] = set()
        ordered: List[Path] = []
        for file_path in dart_files:
            resolved = self._resolve(file_path)
            if resolved is None or resolved in analysed:
                continue
            analysed.add(resolved)
            ordered.append(resolved)
        return analysed, ordered

    def _collect_claims(
        self,
        ordered: List[Path],
        directives: Dict[Path, Tuple[List[str], Optional[str], Optional[str]]],
        analysed: Set[Path],
    ) -> Tuple[Dict[Path, Path], Set[Path]]:
        """Map each soundly claimed part file to its parent.

        Parents are read in turn, so a first claim is provisional until the pass
        completes: a second claimant moves the part from sound to conflicted and removes
        it from the map. Returns the surviving claims and, separately, every part some
        parent claimed — including claims this pass dropped.
        """
        claims: Dict[Path, Path] = {}
        first_claimant: Dict[Path, Path] = {}

        for parent_path in ordered:
            part_uris, _, _ = directives[parent_path]
            for uri in part_uris:
                resolved = self._resolve(parent_path.parent / uri)
                if resolved is None or resolved not in analysed:
                    # The ordinary Flutter shape, not an error: a generated part the scanner
                    # excluded, or a path naming no analysed file (including a `package:` URI).
                    logger.debug(
                        f"Dart part '{uri}' declared by {self._relative(parent_path)} "
                        f"is not in the analysed set"
                    )
                    self._records.append(
                        UnresolvedPartRecord(
                            part_path=uri,
                            reason=REASON_NOT_IN_ANALYSED_SET,
                            parent_path=self._relative(parent_path),
                        )
                    )
                    continue

                owner = first_claimant.get(resolved)
                if owner is not None and owner != parent_path:
                    # Dart forbids this, so the source is in error and the
                    # product does not pick a winner.
                    self._records.append(
                        UnresolvedPartRecord(
                            part_path=self._relative(resolved),
                            reason=REASON_CLAIMED_BY_TWO_PARENTS,
                            parent_path=self._relative(owner),
                            second_parent_path=self._relative(parent_path),
                        )
                    )
                    claims.pop(resolved, None)
                    continue

                first_claimant.setdefault(resolved, parent_path)
                claims[resolved] = parent_path

        return claims, set(first_claimant)

    def _drop_chains(self, claims: Dict[Path, Path], parents: Set[Path]) -> None:
        """Remove every claim on both ends of a chain, and record it.

        A file that is both a resolved part and a parent of its own parts would make a
        component in the inner part eligible for two prefixes, so application order would
        decide its identity. One pass suffices: a chain's two ends are both claims, and
        dropping either end drops the chain.
        """
        chained = {part for part in claims if part in parents}
        if not chained:
            return
        for part, parent in list(claims.items()):
            if part not in chained and parent not in chained:
                continue
            self._records.append(
                UnresolvedPartRecord(
                    part_path=self._relative(part),
                    reason=REASON_CHAINED_PART,
                    parent_path=self._relative(parent),
                )
            )
            claims.pop(part, None)

    def _record_orphans(
        self,
        ordered: List[Path],
        directives: Dict[Path, Tuple[List[str], Optional[str], Optional[str]]],
        claimed_ever: Set[Path],
    ) -> None:
        """Record a ``part of`` that no parent in the analysed set claims.

        ``claimed_ever`` holds every part a directive resolved to, including claims later
        dropped as conflicted or chained. A conflicted or chained part is NOT an orphan —
        its parent IS in the analysed set — and recording it as one would be false.
        """
        for path in ordered:
            _, part_of, _ = directives[path]
            if part_of is None or path in claimed_ever:
                continue
            # Recorded only, never prefixed: the only way to prefix an orphan is to
            # trust a `part of` path to a parent outside the analysed set. What it
            # names is carried as data so a reader can tell a typo from an excluded file.
            self._records.append(
                UnresolvedPartRecord(
                    part_path=self._relative(path),
                    reason=REASON_ORPHANED_PART,
                    declared_parent_path=part_of or None,
                )
            )

    def _prefix_for(self, parent_path: Path) -> Optional[str]:
        """Encoded repo-relative path of a library's parent.

        A literal ``$`` in a segment is written ``$$``, then segments are joined with the
        TWO-character separator ``$-``. Since every ``$`` a segment contributes is
        doubled, a segment can never spell ``$-``, so the encoding is injective. Two
        simpler encodings are not: a dotted join merges ``src/a.b.dart`` with
        ``src/a/b.dart``, and a ``/`` join is unaddressable through the query API.
        """
        try:
            relative = parent_path.relative_to(self._source_directory)
        except ValueError:
            return None
        parts = list(relative.parts)
        if not parts:
            return None
        written = ""
        last = parts[-1]
        if last.lower().endswith(_DART_EXTENSION):
            cut = len(last) - len(_DART_EXTENSION)
            parts[-1] = last[:cut]
            if last[cut:] != _DART_EXTENSION:
                # Appended AFTER escaping, so its `$` stays single and no real
                # segment can spell it.
                written = f"${last[cut:]}"
        return "$-".join(part.replace("$", "$$") for part in parts) + written

    def _relative(self, path: Path) -> str:
        """Repo-relative path for a record, falling back to the full path."""
        try:
            return str(path.relative_to(self._source_directory))
        except ValueError:
            return str(path)

    def _resolve(self, path: Path) -> Optional[Path]:
        """Resolve a path, tolerating a filesystem that refuses to answer."""
        try:
            return path.resolve()
        except OSError:
            return None

    def _read_directives(self, path: Path) -> Tuple[List[str], Optional[str], Optional[str]]:
        """Read one file's leading directive section.

        A narrow line scan rather than a parse. Dart requires directives to precede all
        declarations, so the scan stops at the first declaration and never reads the rest
        of the file. Returns ``(part URIs, part-of target, library name)``.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug(f"Could not read Dart directives from {path}")
            return [], None, None

        # A byte-order mark sits before the first directive; strip it so the
        # first statement's keyword is recognisable. splitlines() handles CRLF
        # and lone CR.
        text = text.lstrip("\ufeff")

        part_uris: List[str] = []
        part_of: Optional[str] = None
        library_name: Optional[str] = None

        depth = 0
        buffer = ""
        at_start = True
        for line in text.splitlines():
            cleaned, depth = self._strip_comments(line, depth)
            cleaned = cleaned.strip()
            if not cleaned:
                continue
            if at_start and cleaned.startswith("#!"):
                # A script line carries no terminator and is not a statement, so
                # letting it reach the directive guard ends the scan before any
                # directive is read (its first token is empty).
                at_start = False
                continue
            at_start = False
            if not buffer and not self._may_begin_directive(cleaned):
                break
            buffer = f"{buffer} {cleaned}".strip() if buffer else cleaned
            while ";" in buffer:
                statement, _, buffer = buffer.partition(";")
                statement = statement.strip()
                buffer = buffer.strip()
                if not statement:
                    continue
                # An annotation may precede a directive (`@Deprecated('old')`
                # above `library`), and it carries no terminator of its own, so
                # it arrives joined to the statement it decorates.
                statement = self._strip_leading_annotations(statement)
                if not statement:
                    continue
                if _PART_OF_RE.match(statement):
                    if part_of is None:
                        target = statement[len("part of") :].strip()
                        # A URI when quoted, a library name on the legacy form.
                        part_of = self._read_uri(target) or target
                    continue
                part_match = _PART_KEYWORD_RE.match(statement)
                if part_match:
                    uri = self._read_uri(part_match.group("rest"))
                    if uri:
                        part_uris.append(uri)
                    else:
                        # A form this line scan cannot read — a triple-quoted or interpolated
                        # URI, say. Recording nothing is right: a half-read target would name a
                        # file another parent legitimately claims and fake a two-parent conflict.
                        logger.debug(f"Unreadable Dart part directive in {path}: {statement!r}")
                    continue
                library_match = _LIBRARY_RE.match(statement)
                if library_match:
                    library_name = library_match.group("name")

        return part_uris, part_of, library_name

    def _read_uri(self, text: str) -> Optional[str]:
        """Read a directive's URI: one string literal, or several concatenated.

        Dart allows a raw string (``r'a.dart'``) and allows adjacent literals to
        concatenate (``'a' '.dart'``). A single regex ending at the first quote gets both
        wrong — the first does not match, the second matches a garbled target.
        """
        text = text.strip()
        pieces: List[str] = []
        index = 0
        while index < len(text):
            scanned = self._scan_string_literal(text, index)
            if scanned is None:
                return None
            content, index = scanned
            pieces.append(content)
            while index < len(text) and text[index].isspace():
                index += 1
        if not pieces:
            return None
        return "".join(pieces)

    def _scan_string_literal(self, text: str, start: int) -> Optional[Tuple[str, int]]:
        """One Dart string literal at ``start`` as ``(content, index after it)``.

        A leading ``r`` makes the literal raw, where a backslash is an ordinary
        character. In a non-raw literal a backslash escapes the character after it,
        which is what makes ``'od\\'d.dart'`` name ``od'd.dart``. Dart's control and
        code-point escapes (``\\n``, ``\\x41``, ``\\u{43}``) are deliberately NOT
        resolved: this content is compared against a path on disk, and no file is
        named by one.
        """
        index = start
        raw = False
        if index < len(text) and text[index] == "r":
            raw = True
            index += 1
        if index >= len(text) or text[index] not in ("'", '"'):
            return None
        quote = text[index]
        index += 1
        out: List[str] = []
        while index < len(text):
            char = text[index]
            if not raw and char == "\\":
                if index + 1 >= len(text):
                    return None
                out.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                return "".join(out), index + 1
            out.append(char)
            index += 1
        return None

    def _strip_leading_annotations(self, statement: str) -> str:
        """Drop any annotations that arrived joined to a directive statement.

        An annotation has no terminator, so ``@Deprecated('old')`` above
        ``library legacy;`` reaches the statement scan as one string.
        """
        while True:
            match = _ANNOTATION_RE.match(statement)
            if not match:
                return statement.strip()
            rest = statement[match.end() :].lstrip()
            if rest.startswith("("):
                end = self._matching_paren(rest)
                if end is None:
                    return rest.strip()
                rest = rest[end + 1 :].lstrip()
            statement = rest

    def _matching_paren(self, text: str) -> Optional[int]:
        """Index of the ``)`` closing ``text[0]``, or None when unbalanced."""
        depth = 0
        quote: Optional[str] = None
        index = 0
        while index < len(text):
            char = text[index]
            if quote is not None:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        return None

    def _may_begin_directive(self, line: str) -> bool:
        """Whether a statement starting on this line can still be a directive.

        An annotation may precede ``library``, so a line starting with ``@``
        keeps the scan open. Anything else that is not a directive keyword begins
        a declaration and ends the directive section.
        """
        if line.startswith("@"):
            return True
        token = re.split(r"[^\w$]", line, maxsplit=1)[0]
        return token in _DIRECTIVE_KEYWORDS

    def _strip_comments(self, line: str, depth: int) -> Tuple[str, int]:
        """Remove comments from one line, respecting string literals.

        A ``part`` inside a comment is not a directive, and a ``//`` inside a quoted URI
        is not a comment start — so the scan tracks quote state rather than cutting at
        the first ``//``. ``depth`` carries block-comment nesting between lines: Dart
        block comments nest, so one closes only at its outermost ``*/``.
        """
        out: List[str] = []
        quote: Optional[str] = None
        index = 0
        length = len(line)
        while index < length:
            char = line[index]
            if depth > 0:
                if char == "/" and index + 1 < length and line[index + 1] == "*":
                    depth += 1
                    index += 2
                    continue
                if char == "*" and index + 1 < length and line[index + 1] == "/":
                    depth -= 1
                    index += 2
                    continue
                index += 1
                continue
            if quote is not None:
                out.append(char)
                if char == "\\":
                    if index + 1 < length:
                        out.append(line[index + 1])
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in ("'", '"'):
                quote = char
                out.append(char)
                index += 1
                continue
            if char == "/" and index + 1 < length and line[index + 1] == "/":
                break
            if char == "/" and index + 1 < length and line[index + 1] == "*":
                depth += 1
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out), depth
