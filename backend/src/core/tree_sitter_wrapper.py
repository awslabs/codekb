"""
Tree-sitter integration wrapper for the Code Knowledge Base.

This module provides a wrapper around Tree-sitter parsers for parsing
source code files and extracting structural information.
"""

from pathlib import Path
from typing import Dict, Any, Set, Optional
import re

try:
    from tree_sitter import Language, Parser
    import tree_sitter_python

    try:
        import tree_sitter_typescript

        TYPESCRIPT_AVAILABLE = True
    except ImportError:
        tree_sitter_typescript = None
        TYPESCRIPT_AVAILABLE = False
    try:
        import tree_sitter_javascript

        JAVASCRIPT_AVAILABLE = True
    except ImportError:
        tree_sitter_javascript = None
        JAVASCRIPT_AVAILABLE = False
    try:
        import tree_sitter_java

        JAVA_AVAILABLE = True
    except ImportError:
        tree_sitter_java = None
        JAVA_AVAILABLE = False
    try:
        import tree_sitter_kotlin

        KOTLIN_AVAILABLE = True
    except ImportError:
        tree_sitter_kotlin = None
        KOTLIN_AVAILABLE = False
    try:
        import tree_sitter_go

        GO_AVAILABLE = True
    except ImportError:
        tree_sitter_go = None
        GO_AVAILABLE = False
    try:
        import tree_sitter_c_sharp

        CSHARP_AVAILABLE = True
    except ImportError:
        tree_sitter_c_sharp = None
        CSHARP_AVAILABLE = False
    try:
        import tree_sitter_rust

        RUST_AVAILABLE = True
    except ImportError:
        tree_sitter_rust = None
        RUST_AVAILABLE = False
    try:
        import tree_sitter_cpp

        CPP_AVAILABLE = True
    except ImportError:
        tree_sitter_cpp = None
        CPP_AVAILABLE = False
    try:
        import tree_sitter_c

        C_AVAILABLE = True
    except ImportError:
        tree_sitter_c = None
        C_AVAILABLE = False
    try:
        import tree_sitter_php

        PHP_AVAILABLE = True
    except ImportError:
        tree_sitter_php = None
        PHP_AVAILABLE = False
    try:
        import tree_sitter_ruby

        RUBY_AVAILABLE = True
    except ImportError:
        tree_sitter_ruby = None
        RUBY_AVAILABLE = False
    try:
        import tree_sitter_hcl

        HCL_AVAILABLE = True
    except ImportError:
        tree_sitter_hcl = None
        HCL_AVAILABLE = False
    try:
        import tree_sitter_dart

        DART_AVAILABLE = True
    except ImportError:
        tree_sitter_dart = None
        DART_AVAILABLE = False
    TREE_SITTER_AVAILABLE = True
except ImportError as e:
    TREE_SITTER_AVAILABLE = False
    tree_sitter_python = None
    tree_sitter_typescript = None
    tree_sitter_javascript = None
    tree_sitter_java = None
    tree_sitter_kotlin = None
    tree_sitter_go = None
    tree_sitter_c_sharp = None
    tree_sitter_rust = None
    tree_sitter_php = None
    tree_sitter_ruby = None
    tree_sitter_hcl = None
    tree_sitter_cpp = None
    tree_sitter_c = None
    tree_sitter_dart = None
    TYPESCRIPT_AVAILABLE = False
    JAVASCRIPT_AVAILABLE = False
    JAVA_AVAILABLE = False
    KOTLIN_AVAILABLE = False
    GO_AVAILABLE = False
    CSHARP_AVAILABLE = False
    RUST_AVAILABLE = False
    PHP_AVAILABLE = False
    RUBY_AVAILABLE = False
    HCL_AVAILABLE = False
    CPP_AVAILABLE = False
    C_AVAILABLE = False
    DART_AVAILABLE = False
    Language = None
    Parser = None
    import logging as _logging

    _logging.getLogger(__name__).warning(f"Tree-sitter not available: {e}")

from .models import ParseDiagnostic, ParseDiagnosticKind, ParseResult, IdScheme, DEFAULT_SCHEME
from .config import get_config, get_positive_int
from .logging_utils import get_logger, ErrorContext, ParseError
from ..parsers import ParserFactory, BaseLanguageParser


logger = get_logger(__name__)


# ── C/C++ macro sanitization ─────────────────────────────────────────────────
# tree-sitter cannot expand C preprocessor macros. Several common macros sit in
# declaration positions and derail parsing (dropping classes/members or emitting
# spurious ones). We neutralize them with a length-preserving pre-clean: matched
# spans are overwritten with spaces so every byte offset, column, and line number
# is preserved (component line ranges stay correct). Gated to c/cpp only.

# Clang thread-safety annotations that trail a member/method declarator, e.g.
#   int locked_ GUARDED_BY(mu_);
#   void Insert() EXCLUSIVE_LOCKS_REQUIRED(mu_);
# A curated set is used (rather than "any ALLCAPS(...)"), so macro *statements*
# inside function bodies (CHECK(x); RETURN_IF_ERROR(y);) are left untouched.
_TS_ANNOTATIONS = (
    "GUARDED_BY",
    "PT_GUARDED_BY",
    "EXCLUSIVE_LOCKS_REQUIRED",
    "SHARED_LOCKS_REQUIRED",
    "LOCKS_EXCLUDED",
    "EXCLUSIVE_LOCK_FUNCTION",
    "SHARED_LOCK_FUNCTION",
    "UNLOCK_FUNCTION",
    "EXCLUSIVE_TRYLOCK_FUNCTION",
    "SHARED_TRYLOCK_FUNCTION",
    "ASSERT_EXCLUSIVE_LOCK",
    "ASSERT_SHARED_LOCK",
    "ACQUIRE",
    "ACQUIRE_SHARED",
    "RELEASE",
    "RELEASE_SHARED",
    "ACQUIRED_BEFORE",
    "ACQUIRED_AFTER",
    "REQUIRES",
    "REQUIRES_SHARED",
    "TRY_ACQUIRE",
    "TRY_ACQUIRE_SHARED",
    "NO_THREAD_SAFETY_ANALYSIS",
)

# `class LEVELDB_EXPORT Iterator {` / `struct SCOPED_LOCKABLE MutexLock {` — an
# ALLCAPS macro token wedged between class/struct and the real type name.
#
# The lookahead requires a genuine class-definition head after the real name:
# the name must be followed by a base-clause `:` or an opening `{` (optionally
# `final` in between). This is what distinguishes a decorated class definition
# from an elaborated-type variable declaration such as `struct GLOBAL_STATE
# state;` (a real all-caps struct tag + variable), which must NOT be touched.
#
# Note: only the first ALLCAPS token is blanked; a rare double-macro head like
# `class API FINAL Foo {` is left alone (re.sub is a single pass) rather than
# risk mangling a real name.
_CLASS_MACRO_RE = re.compile(
    rb"\b(class|struct)([ \t]+)([A-Z_][A-Z0-9_]+)([ \t]+)"
    rb"(?=[A-Za-z_]\w*(?:[ \t\r\n]+final\b)?[ \t\r\n]*[:{])"
)
_DECLSPEC_RE = re.compile(rb"__declspec[ \t]*\([^()]*\)")
_ATTRIBUTE_RE = re.compile(rb"__attribute__[ \t]*\(\([^{};]*?\)\)")
_TS_ANNOT_RE = re.compile(
    rb"\b(?:" + b"|".join(a.encode() for a in _TS_ANNOTATIONS) + rb")[ \t]*\([^;{}]*\)"
)


def _blank(span: bytes) -> bytes:
    """Overwrite a byte span with spaces, preserving newlines (and length)."""
    return bytes(b if b in (0x0A, 0x0D) else 0x20 for b in span)


def _sanitize_c_family_source(source: bytes) -> bytes:
    """Length-preserving neutralization of declaration-position C/C++ macros."""
    try:
        # 1. Class/struct-position modifier macro: blank only the macro token.
        source = _CLASS_MACRO_RE.sub(
            lambda m: m.group(1) + m.group(2) + _blank(m.group(3)) + m.group(4),
            source,
        )
        # 2. Compiler visibility attributes anywhere.
        source = _DECLSPEC_RE.sub(lambda m: _blank(m.group(0)), source)
        source = _ATTRIBUTE_RE.sub(lambda m: _blank(m.group(0)), source)
        # 3. Trailing Clang thread-safety annotation macros.
        source = _TS_ANNOT_RE.sub(lambda m: _blank(m.group(0)), source)
    except Exception:
        # Never let sanitization break parsing; fall back to the raw source.
        return source
    return source


def tree_cache_key(file_path, language) -> str:
    """Key for the per-file parsed-AST cache.

    Shared by CodeAnalyzer (which seeds the cache with phase-1 parsed trees)
    and FlowAnalyzer (which reads it during call extraction) so a file is
    parsed at most once per run. The key embeds the full file path, so
    same-named files in different packages do not collide.
    """
    return f"{file_path}:{language}"


class TreeSitterWrapper:
    """
    Wrapper for Tree-sitter parsers providing unified parsing interface.

    Handles parser initialization, code parsing, and component extraction
    for supported programming languages.

    Singleton: Only one instance exists system-wide.
    """

    _instance = None
    _initialized = False

    # Per-run id context (class-level so it survives the singleton's early-return
    # __init__). Set via set_id_context before parsing a space; injected into each
    # language parser right before component extraction.
    _run_root: Optional[Path] = None
    _run_scheme: IdScheme = DEFAULT_SCHEME

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def set_id_context(self, root: Optional[Path], scheme: IdScheme) -> None:
        """Set the root and id scheme used for component-id generation.

        Must be called before parsing a space so freshly parsed ids match the
        space's recorded scheme (new spaces: DEFAULT_SCHEME; existing spaces:
        the scheme resolved from their persisted knowledge base).
        """
        self._run_root = root
        self._run_scheme = scheme

    def __init__(self):
        """Initialize Tree-sitter wrapper."""
        if self._initialized:
            return

        if not TREE_SITTER_AVAILABLE:
            raise ImportError("Tree-sitter libraries not available. Install tree-sitter-python.")

        self.parsers: Dict[str, Any] = {}
        self.language_parsers: Dict[str, BaseLanguageParser] = {}
        self.supported_languages = get_config("analysis.supported_languages", {})
        self.max_ast_depth = get_positive_int("analysis.max_ast_depth")
        self._initialize_parsers()
        TreeSitterWrapper._initialized = True

    def _initialize_parsers(self) -> None:
        """Initialize Tree-sitter parsers for supported languages."""
        logger.info("Initializing Tree-sitter parsers")

        # Initialize Python parser
        if "python" in self.supported_languages:
            try:
                python_language = Language(tree_sitter_python.language())
                python_parser = Parser(python_language)
                self.parsers["python"] = python_parser
                self.language_parsers["python"] = ParserFactory.create_parser("python")
                logger.debug("Initialized Python parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Python parser: {e}")
                raise ImportError(f"Could not initialize Python parser: {e}")

        # Initialize TypeScript parser
        if "typescript" in self.supported_languages and TYPESCRIPT_AVAILABLE:
            try:
                ts_language = Language(tree_sitter_typescript.language_typescript())
                ts_parser = Parser(ts_language)
                self.parsers["typescript"] = ts_parser
                self.language_parsers["typescript"] = ParserFactory.create_parser("typescript")
                logger.debug("Initialized TypeScript parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize TypeScript parser: {e}")

        # Initialize JavaScript parser
        if "javascript" in self.supported_languages and JAVASCRIPT_AVAILABLE:
            try:
                js_language = Language(tree_sitter_javascript.language())
                js_parser = Parser(js_language)
                self.parsers["javascript"] = js_parser
                self.language_parsers["javascript"] = ParserFactory.create_parser("javascript")
                logger.debug("Initialized JavaScript parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize JavaScript parser: {e}")

        # Initialize Java parser
        if "java" in self.supported_languages and JAVA_AVAILABLE:
            try:
                java_language = Language(tree_sitter_java.language())
                java_parser = Parser(java_language)
                self.parsers["java"] = java_parser
                self.language_parsers["java"] = ParserFactory.create_parser("java")
                logger.debug("Initialized Java parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Java parser: {e}")

        # Initialize Kotlin parser
        if "kotlin" in self.supported_languages and KOTLIN_AVAILABLE:
            try:
                kotlin_language = Language(tree_sitter_kotlin.language())
                kotlin_parser = Parser(kotlin_language)
                self.parsers["kotlin"] = kotlin_parser
                self.language_parsers["kotlin"] = ParserFactory.create_parser("kotlin")
                logger.debug("Initialized Kotlin parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Kotlin parser: {e}")

        # Initialize Go parser
        if "go" in self.supported_languages and GO_AVAILABLE:
            try:
                go_language = Language(tree_sitter_go.language())
                go_parser = Parser(go_language)
                self.parsers["go"] = go_parser
                self.language_parsers["go"] = ParserFactory.create_parser("go")
                logger.debug("Initialized Go parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Go parser: {e}")

        # Initialize C# parser
        if "csharp" in self.supported_languages and CSHARP_AVAILABLE:
            try:
                csharp_language = Language(tree_sitter_c_sharp.language())
                csharp_parser = Parser(csharp_language)
                self.parsers["csharp"] = csharp_parser
                self.language_parsers["csharp"] = ParserFactory.create_parser("csharp")
                logger.debug("Initialized C# parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize C# parser: {e}")

        # Initialize Rust parser
        if "rust" in self.supported_languages and RUST_AVAILABLE:
            try:
                rust_language = Language(tree_sitter_rust.language())
                rust_parser = Parser(rust_language)
                self.parsers["rust"] = rust_parser
                self.language_parsers["rust"] = ParserFactory.create_parser("rust")
                logger.debug("Initialized Rust parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Rust parser: {e}")

        # Initialize C++ parser
        if "cpp" in self.supported_languages and CPP_AVAILABLE:
            try:
                cpp_language = Language(tree_sitter_cpp.language())
                cpp_parser = Parser(cpp_language)
                self.parsers["cpp"] = cpp_parser
                self.language_parsers["cpp"] = ParserFactory.create_parser("cpp")
                logger.debug("Initialized C++ parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize C++ parser: {e}")

        # Initialize C parser (uses the C grammar; shares the C/C++ parser class)
        if "c" in self.supported_languages and C_AVAILABLE:
            try:
                c_language = Language(tree_sitter_c.language())
                c_parser = Parser(c_language)
                self.parsers["c"] = c_parser
                self.language_parsers["c"] = ParserFactory.create_parser("c")
                logger.debug("Initialized C parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize C parser: {e}")

        # Initialize PHP parser
        if "php" in self.supported_languages and PHP_AVAILABLE:
            try:
                php_language = Language(tree_sitter_php.language_php())
                php_parser = Parser(php_language)
                self.parsers["php"] = php_parser
                self.language_parsers["php"] = ParserFactory.create_parser("php")
                logger.debug("Initialized PHP parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize PHP parser: {e}")

        # Initialize Ruby parser
        if "ruby" in self.supported_languages and RUBY_AVAILABLE:
            try:
                ruby_language = Language(tree_sitter_ruby.language())
                ruby_parser = Parser(ruby_language)
                self.parsers["ruby"] = ruby_parser
                self.language_parsers["ruby"] = ParserFactory.create_parser("ruby")
                logger.debug("Initialized Ruby parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Ruby parser: {e}")

        # Initialize Terraform (HCL) parser
        if "terraform" in self.supported_languages and HCL_AVAILABLE:
            try:
                hcl_language = Language(tree_sitter_hcl.language())
                hcl_parser = Parser(hcl_language)
                self.parsers["terraform"] = hcl_parser
                self.language_parsers["terraform"] = ParserFactory.create_parser("terraform")
                logger.debug("Initialized Terraform parser successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Terraform parser: {e}")

        # The PyPI grammar lags the language: it cannot parse Dart 3.13
        # declaring constructors. Those declarations are refused by the
        # name-trust check rather than mis-named. See docs/languages.md.
        if "dart" in self.supported_languages and DART_AVAILABLE:
            try:
                dart_language = Language(tree_sitter_dart.language())
                dart_parser = Parser(dart_language)
                self.parsers["dart"] = dart_parser
                self.language_parsers["dart"] = ParserFactory.create_parser("dart")
                logger.debug("Initialized Dart parser successfully")
            except Exception as e:
                # Logged, never raised: only Python is load-bearing here, and a
                # missing or broken Dart grammar must leave .dart files reported
                # as skipped rather than failing the run.
                logger.error(f"Failed to initialize Dart parser: {e}")

        if not self.parsers:
            raise ImportError("No Tree-sitter parsers could be initialized")

        # Configured but not loaded. Nothing else reports this: the language's
        # block is guarded by its availability flag and the scanner then narrows
        # to the loaded set, so a missing grammar reaches a green build silently.
        unloaded = sorted(set(self.supported_languages) - set(self.parsers))
        if unloaded:
            logger.warning(
                f"Configured languages with no loaded parser: {', '.join(unloaded)}. "
                f"Files in these languages are reported as skipped, not parsed. "
                f"A compiled grammar built for another interpreter or architecture "
                f"fails this way."
            )

        logger.info(f"Initialized {list(self.parsers.keys())} Tree-sitter parsers")

    def parse_file(self, file_path: Path, language: str) -> ParseResult:
        """
        Parse a source code file using Tree-sitter.

        Args:
            file_path: Path to the source file
            language: Programming language identifier

        Returns:
            ParseResult containing extracted components and metadata

        Raises:
            ParseError: If parsing fails
        """
        with ErrorContext(logger, f"parse file {file_path}"):
            if language not in self.parsers:
                raise ParseError(f"No parser available for language: {language}", file_path)

            # Read file content
            try:
                with open(file_path, "rb") as file:
                    source_code = file.read()
            except Exception as e:
                raise ParseError(f"Failed to read file: {e}", file_path)

            # C/C++ only: neutralize declaration-position macros that tree-sitter
            # cannot expand (API export macros, Clang thread-safety annotations).
            # Length-preserving, so byte offsets and line numbers are unchanged.
            if language in ("c", "cpp"):
                source_code = _sanitize_c_family_source(source_code)

            # Parse with Tree-sitter
            parser = self.parsers[language]
            try:
                tree = parser.parse(source_code)
                root_node = tree.root_node
            except Exception as e:
                raise ParseError(f"Tree-sitter parsing failed: {e}", file_path)

            # Extract components using language-specific parser
            components = []
            parse_errors = []
            parser_diagnostics: list[ParseDiagnostic] = []

            try:
                if language in self.language_parsers:
                    if self._tree_exceeds_depth(root_node, self.max_ast_depth):
                        message = (
                            f"Syntax tree exceeds maximum depth ({self.max_ast_depth}); "
                            "skipping component extraction to avoid stack exhaustion"
                        )
                        parse_errors.append(message)
                        logger.warning(f"{message}: {file_path}")
                    else:
                        language_parser = self.language_parsers[language]
                        # Inject the per-run id context so make_id uses this
                        # space's scheme/root (covers the cached parser instances).
                        language_parser._root = self._run_root
                        language_parser._scheme = self._run_scheme
                        try:
                            components = language_parser.extract_components(
                                root_node, source_code, file_path
                            )
                        finally:
                            # Drained in a finally so what the parser recorded
                            # before it raised is still reported: a refusal to
                            # name a declaration must not be lost to a later
                            # exception.
                            parser_diagnostics = language_parser.drain_parse_diagnostics()
                else:
                    logger.warning(f"Component extraction not implemented for language: {language}")
            except Exception as e:
                parse_errors.append(f"Component extraction failed: {e}")
                logger.error(f"Component extraction error in {file_path}: {e}")

            # Create parse result
            # The node count and the structural diagnostics come from ONE walk:
            # this was already visiting every node for `node_count`, so collection
            # is folded in rather than added as a second walk.
            node_count, diagnostics = self._count_nodes_and_diagnostics(root_node, file_path)
            if parser_diagnostics:
                # One channel, kept in document order: a reader of this list
                # does not have to know which producer found what.
                diagnostics.extend(parser_diagnostics)
                diagnostics.sort(key=lambda d: (d.byte_offset, d.kind.value))

            result = ParseResult(
                file_path=file_path,
                language=language,
                components=components,
                parse_errors=parse_errors,
                syntax_tree=root_node,
                metadata={
                    "parser_version": getattr(tree_sitter_python, "__version__", "unknown")
                    if language == "python"
                    else getattr(tree_sitter_typescript, "__version__", "unknown")
                    if language == "typescript" and TYPESCRIPT_AVAILABLE
                    else "unknown",
                    "source_size": len(source_code),
                    "node_count": node_count,
                },
                diagnostics=diagnostics,
            )

            logger.debug(
                f"Parsed {file_path}: {len(components)} components, {len(parse_errors)} errors"
            )
            return result

    def _tree_exceeds_depth(self, node: Any, limit: int) -> bool:
        """Report whether the syntax tree nests deeper than ``limit``.

        Uses an explicit iterative stack (not recursion) so that checking an
        untrusted, deeply nested tree cannot itself exhaust the call stack.
        Returns as soon as the limit is passed, so a shallow tree is cheap.
        """
        stack = [(node, 1)]
        while stack:
            current, depth = stack.pop()
            if depth > limit:
                return True
            for child in current.children:
                stack.append((child, depth + 1))
        return False

    def _count_nodes(self, node: Any) -> int:
        """Count total number of nodes in syntax tree.

        Traversal is iterative to prevent exhaustion of call stack.
        """
        count = 0
        stack = [node]
        while stack:
            current = stack.pop()
            count += 1
            stack.extend(current.children)
        return count

    def _count_nodes_and_diagnostics(
        self, node: Any, file_path: Path
    ) -> "tuple[int, list[ParseDiagnostic]]":
        """Count the tree's nodes and collect its structural defects in one walk.

        Runs for every language with no per-language branch, or the measurement
        would not be comparable between languages. Nothing is raised: a defect is
        recorded and the parse completes.

        Args:
            node: Root node of the (sub)tree to walk
            file_path: The parsed file, recorded on each diagnostic

        Returns:
            (total node count, diagnostics in document order)
        """
        diagnostics: list[ParseDiagnostic] = []
        count = 0
        # Explicit stack rather than recursion: a deeply nested generated file
        # could exhaust the interpreter's stack, and the walk now also has to
        # survive error recovery.
        stack: list[Any] = [node]
        while stack:
            current = stack.pop()
            count += 1
            if current.is_error or current.is_missing:
                start = current.start_point
                diagnostics.append(
                    ParseDiagnostic(
                        file_path=file_path,
                        kind=ParseDiagnosticKind.ERROR
                        if current.is_error
                        else ParseDiagnosticKind.MISSING,
                        line=start[0] + 1,
                        column=start[1],
                        byte_offset=current.start_byte,
                    )
                )
            children = current.children
            # Pushed reversed so the pop order is document order, which keeps
            # the diagnostics list stable and readable.
            stack.extend(reversed(children))
        diagnostics.sort(key=lambda d: (d.byte_offset, d.kind.value))
        return count, diagnostics

    def get_supported_languages(self) -> Set[str]:
        """
        Get set of supported programming languages.

        Returns:
            Set of supported language identifiers
        """
        return set(self.parsers.keys())

    def is_language_supported(self, language: str) -> bool:
        """
        Check if a programming language is supported.

        Args:
            language: Language identifier to check

        Returns:
            True if language is supported
        """
        return language in self.parsers
