"""
File system utilities for the Code Knowledge Base.

This module provides file system operations, directory scanning,
and file type detection utilities using pathlib.
"""

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import List, Set, Optional, Iterator, Dict, Any, NamedTuple, Pattern
from .config import get_config, require_config
from .logging_utils import get_logger, ErrorContext


logger = get_logger(__name__)

# Prefix for a directory that discard_directory has detached but not yet
# deleted. A leading dot keeps these out of the space-id namespace (ids are
# slugified to [a-z0-9-]), so the startup sweep can glob for them without any
# risk of matching live data.
DISCARDING_PREFIX = ".discarding-"

# The default.yaml exclusion patterns from before spaces recorded the patterns
# they were created with. Applied to those historical spaces, whose exclusion
# patterns stay fixed for their lifetime.
#
# FROZEN: this list MUST NOT be changed, even when default.yaml changes.
# Editing it would silently change the patterns of every historical space.
LEGACY_DEFAULT_EXCLUDE_PATTERNS: List[str] = [
    ".git/",
    ".svn/",
    ".hg/",
    "node_modules/",
    "__pycache__/",
    "__MACOSX/",
    ".pytest_cache/",
    ".mypy_cache/",
    "build/",
    "dist/",
    ".venv/",
    "venv/",
    ".env",
    "*.egg-info",
    "._*",
    "test*",
]


class _ExcludeRule(NamedTuple):
    """A compiled exclusion pattern.

    Attributes:
        regex: Anchored regex the target string must fully match.
        match_basename: When True the regex is tested against a path's basename
            (a pattern with no ``/``, matching at any depth). When False it is
            tested against the path relative to the scan root (a path pattern).
        dir_only: When True the rule only excludes directories (pattern ended
            with ``/``).
        source: The original pattern text, for logging/debugging.
    """

    regex: Pattern[str]
    match_basename: bool
    dir_only: bool
    source: str


def _glob_to_regex(glob: str) -> str:
    """Translate a glob (with gitignore-style ``**``) into a regex body.

    Segment-aware: ``*`` and ``?`` never cross ``/``; ``**`` does. Specifically:
      ``**/`` -> zero or more leading path segments
      ``/**`` (trailing) or bare ``**`` -> anything, including ``/``
      ``*`` -> any run of non-separator characters
      ``?`` -> a single non-separator character
    All other characters are matched literally.
    """
    out: List[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                # '**' — spans directories
                if i + 2 < n and glob[i + 2] == "/":
                    out.append("(?:.*/)?")  # '**/' -> optional leading segments
                    i += 3
                else:
                    out.append(".*")  # bare or trailing '**'
                    i += 2
            else:
                out.append("[^/]*")  # single-segment wildcard
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


class FileScanner:
    """
    File system scanner for discovering source code files.

    Handles recursive directory scanning with exclusion patterns
    and file type filtering based on configuration.
    """

    def __init__(self, exclude_patterns: Optional[List[str]] = None):
        """
        Initialize file scanner.

        Args:
            exclude_patterns: Optional list of exclusion patterns. ``None`` uses
                the configured patterns; an empty list excludes nothing.
        """
        self.exclude_patterns = (
            exclude_patterns
            if exclude_patterns is not None
            else get_config("analysis.exclude_patterns", [])
        )
        # Precompile patterns once; matching then just runs anchored regexes.
        self._exclude_rules: List[_ExcludeRule] = [
            rule
            for pattern in self.exclude_patterns
            if (rule := self._compile_exclude(pattern)) is not None
        ]
        self.supported_languages = get_config("analysis.supported_languages", {})
        self.max_file_size_mb = require_config("analysis.max_file_size_mb")
        self.max_files_per_directory = require_config("analysis.max_files_per_directory")

        # Map of known-but-unsupported source extensions -> language name, used
        # to report skipped files (e.g. Swift, Scala) without counting docs,
        # config, or asset files. Populated from analysis.unsupported_languages.
        self.unsupported_languages = get_config("analysis.unsupported_languages", {})
        self._unsupported_extension_map: Dict[str, str] = {}
        for language, lang_config in self.unsupported_languages.items():
            for extension in lang_config.get("extensions", []):
                self._unsupported_extension_map[extension.lower()] = language

        # Populated by scan_directory: language name -> count of files skipped
        # because CodeKB has no parser for that language.
        self.skipped_files: Dict[str, int] = {}

        # Per-language generated-file patterns from the config. A language
        # carrying no list gets an empty one, which excludes nothing, so a
        # language without the key behaves exactly as it did before. Compiled
        # with the same glob translation as the exclusion rules, matched against
        # a basename.
        self._generated_file_patterns: Dict[str, List[str]] = {}
        self._generated_file_rules: Dict[str, List[Pattern[str]]] = {}
        for language, lang_config in self.supported_languages.items():
            patterns = lang_config.get("generated_file_patterns") or []
            self._generated_file_patterns[language] = list(patterns)
            self._generated_file_rules[language] = [
                re.compile(f"^{_glob_to_regex(pattern)}$") for pattern in patterns
            ]

        self.generated_files: Dict[str, int] = {}
        self.generated_files_reason = "generated"

        # Languages CodeKB can actually parse (the loaded Tree-sitter parsers).
        # The config's supported_languages can list languages that don't have a
        # working parser yet, so relying on config alone would let those files
        # slip through as neither analyzed nor skipped. When set, only files in
        # these languages are analyzed; any other recognizable source file is
        # reported as skipped. When None (the default), the scanner falls back
        # to the config list.
        self.available_languages: Optional[Set[str]] = None

    def set_available_languages(self, languages: Optional[Set[str]]) -> None:
        """Restrict analyzable languages to those with a working parser.

        Args:
            languages: Set of language identifiers that actually have a loaded
                parser. Pass None to fall back to the configured languages.
        """
        self.available_languages = set(languages) if languages is not None else None

    def scan_directory(self, directory_path: Path) -> List[Path]:
        """
        Recursively scan directory for source code files.

        Args:
            directory_path: Root directory to scan

        Returns:
            List of discovered source file paths

        Raises:
            FileNotFoundError: If directory doesn't exist
            PermissionError: If directory is not accessible
        """
        with ErrorContext(logger, f"scan directory {directory_path}"):
            if not directory_path.exists():
                raise FileNotFoundError(f"Directory not found: {directory_path}")

            if not directory_path.is_dir():
                raise ValueError(f"Path is not a directory: {directory_path}")

            discovered_files: List[Path] = []
            file_count = 0
            skipped_files: Dict[str, int] = {}
            generated_files: Dict[str, int] = {}

            logger.info(f"Scanning directory: {directory_path}")

            for file_path in self._walk_directory(directory_path):
                generated_language = self.generated_language_for(file_path)
                if generated_language:
                    generated_files[generated_language] = (
                        generated_files.get(generated_language, 0) + 1
                    )
                    continue

                if self.should_include_file(file_path):
                    discovered_files.append(file_path)
                    file_count += 1

                    if file_count >= self.max_files_per_directory:
                        logger.warning(
                            f"Reached maximum file limit ({self.max_files_per_directory}), stopping scan"
                        )
                        break
                else:
                    # Not analyzed. If it's a recognizable source file in a
                    # language we can't parse (unsupported, or configured but
                    # without a working parser yet), record it so we
                    # can surface the skip to the user. Non-code files (docs,
                    # config, assets) are deliberately not counted.
                    language = self._skipped_language_for(file_path)
                    if language:
                        skipped_files[language] = skipped_files.get(language, 0) + 1

            self.skipped_files = skipped_files
            self.generated_files = generated_files
            total_skipped = sum(skipped_files.values())
            total_generated = sum(generated_files.values())
            logger.info(
                f"Discovered {len(discovered_files)} source files"
                + (
                    f"; skipped {total_skipped} file(s) in unsupported languages: "
                    f"{', '.join(sorted(skipped_files))}"
                    if skipped_files
                    else ""
                )
                + (
                    f"; excluded {total_generated} generated file(s): "
                    + ", ".join(
                        f"{language} {count} ({self.generated_files_reason})"
                        for language, count in sorted(generated_files.items())
                    )
                    if generated_files
                    else ""
                )
            )
            return discovered_files

    def get_supported_extensions(self) -> Set[str]:
        """
        Get all supported file extensions.

        Returns:
            Set of supported file extensions
        """
        extensions: Set[str] = set()
        for lang_config in self.supported_languages.values():
            extensions.update(lang_config.get("extensions", []))
        return extensions

    def detect_language(self, file_path: Path) -> Optional[str]:
        """
        Detect programming language from file extension.

        Args:
            file_path: Path to the file

        Returns:
            Language identifier or None if not supported
        """
        file_extension = file_path.suffix.lower()

        for language, config in self.supported_languages.items():
            if file_extension in config.get("extensions", []):
                return language

        return None

    def is_supported_file(self, file_path: Path) -> bool:
        """
        Check if file is a supported source code file.

        A file is supported only if its language is configured AND CodeKB has a
        working parser for it. Languages listed in config but without a loaded
        parser (see set_available_languages) are treated as unsupported so they
        get reported as skipped rather than silently failing during parsing.

        Args:
            file_path: Path to check

        Returns:
            True if file is supported
        """
        language = self.detect_language(file_path)
        if language is None:
            return False
        if self.available_languages is not None and language not in self.available_languages:
            return False
        return True

    def _skipped_language_for(self, file_path: Path) -> Optional[str]:
        """Return a language label if this un-analyzed file is recognizable source code.

        Covers two cases:
        - A configured language that has no working parser yet (e.g. a language
          listed in config but whose grammar failed to load),
          detected from the supported_languages extension map.
        - A known language CodeKB doesn't support at all (e.g. Swift, Scala),
          detected from the unsupported_languages extension map.

        Returns None for non-code files (docs, config, assets) so they are not
        counted as skipped.
        """
        language = self.detect_language(file_path)
        if language is not None:
            return language
        return self._unsupported_extension_map.get(file_path.suffix.lower())

    def generated_language_for(self, file_path: Path) -> Optional[str]:
        """Return the language label when this file is machine-generated.

        Generated only when the language is analysable — configured AND with a
        working parser — and the name matches one of its configured patterns.
        Anything else returns None and falls through to the existing path.

        Args:
            file_path: File path to classify

        Returns:
            The language identifier when the file is generated, else None.
        """
        language = self.detect_language(file_path)
        if language is None or not self.is_supported_file(file_path):
            return None

        file_name = file_path.name
        for rule in self._generated_file_rules.get(language, []):
            if rule.match(file_name):
                return language

        return None

    def _walk_directory(self, directory_path: Path) -> Iterator[Path]:
        """
        Walk directory tree yielding file paths.

        Args:
            directory_path: Directory to walk

        Yields:
            File paths in the directory tree
        """
        try:
            for current_root, dirs, files in os.walk(directory_path):
                root_path = Path(current_root)

                # Filter (and prune) directories based on exclusion patterns.
                # Pruning here means excluded directories are never descended,
                # so their whole subtree is skipped.
                # Sorted: os.walk order depends on the file system, and the scan
                # order must not change the analysis result.
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not self._is_excluded(root_path / d, directory_path, is_dir=True)
                )

                # Yield files that pass initial filters
                for file_name in sorted(files):
                    file_path = root_path / file_name
                    if not self._is_excluded(file_path, directory_path, is_dir=False):
                        yield file_path

        except PermissionError as e:
            logger.warning(f"Permission denied accessing directory: {directory_path}")
            raise e

    def should_include_file(self, file_path: Path) -> bool:
        """
        Check if file should be included in analysis.

        Args:
            file_path: File path to check

        Returns:
            True if file should be included
        """
        # Check if it's a supported source file
        if not self.is_supported_file(file_path):
            return False

        # Check file size
        try:
            file_size_mb = file_path.stat().st_size / (1024 * 1024)
            if file_size_mb > self.max_file_size_mb:
                logger.warning(f"Skipping large file ({file_size_mb:.1f}MB): {file_path}")
                return False
        except OSError:
            logger.warning(f"Could not get file size: {file_path}")
            return False

        return True

    @staticmethod
    def _compile_exclude(pattern: str) -> Optional["_ExcludeRule"]:
        """Compile one exclusion pattern into an anchored regex rule.

        Semantics (a practical gitignore subset):
          * A pattern with no ``/`` matches a path's BASENAME at any depth
            (e.g. ``node_modules``, ``*.min.js``) — this is the historical
            behavior and covers every built-in default.
          * A pattern containing ``/`` matches the path RELATIVE TO THE SCAN
            ROOT (e.g. ``static/js/*.min.js``, ``src/generated``). A leading
            ``/`` is an explicit root anchor; otherwise it is anchored to the
            root too, matching gitignore.
          * ``**`` spans directories; ``*``/``?`` stay within one segment.
          * A trailing ``/`` restricts the rule to directories.

        Returns None for an empty/whitespace pattern.
        """
        pattern = pattern.strip()
        if not pattern:
            return None

        dir_only = pattern.endswith("/")
        core = pattern.rstrip("/")
        # Anchoring must be decided BEFORE stripping the leading slash: a slash
        # anywhere (leading or interior) makes the pattern relative to the scan
        # root (gitignore rule). Only a pattern with no slash at all is a
        # basename rule that matches at any depth. Deciding after the strip
        # would misread "/foo" as the basename "foo".
        anchored = "/" in core
        core = core.lstrip("/")  # the leading slash was only an anchor marker
        if not core:
            return None

        match_basename = not anchored
        regex = re.compile(f"^{_glob_to_regex(core)}$")
        return _ExcludeRule(
            regex=regex, match_basename=match_basename, dir_only=dir_only, source=pattern
        )

    def _is_excluded_directory(self, dir_path: Path) -> bool:
        """Basename-only exclusion check for a single directory.

        Kept for callers that walk a tree independently of ``scan_directory``
        (e.g. ``RubyAppBoundaryQualifier``) and hold only a directory path, not
        the scan root. Only basename rules (patterns without ``/``) are applied,
        which is faithful to the exclusion behavior these callers relied on
        before path-relative patterns existed; path rules need a root and are
        evaluated in ``scan_directory`` via ``_is_excluded``.
        """
        name = dir_path.name
        for rule in self._exclude_rules:
            if rule.match_basename and rule.regex.match(name):
                return True
        return False

    def _is_excluded(self, path: Path, root: Path, is_dir: bool) -> bool:
        """Check whether a path is excluded by any compiled rule.

        Args:
            path: The file or directory being tested.
            root: The scan root; path patterns are matched relative to it.
            is_dir: True when ``path`` is a directory (directory-only rules
                are skipped for files).
        """
        name = path.name
        try:
            rel_posix = path.relative_to(root).as_posix()
        except ValueError:
            # Not under root (unexpected); fall back to basename matching only.
            rel_posix = name

        for rule in self._exclude_rules:
            if rule.dir_only and not is_dir:
                continue
            target = name if rule.match_basename else rel_posix
            if rule.regex.match(target):
                return True
        return False

    def is_excluded_path(self, rel_path: str) -> bool:
        """Check a repo-relative file path, including its parent directories.

        For callers that hold paths rather than walking a tree (e.g. the refresh
        diff); ``scan_directory`` prunes excluded directories instead.
        """
        root = Path()
        path = Path(rel_path)
        for parent in reversed(path.parents[:-1]):
            if self._is_excluded(parent, root, is_dir=True):
                return True
        return self._is_excluded(path, root, is_dir=False)


def ensure_directory_exists(directory_path: Path) -> None:
    """
    Ensure directory exists, creating it if necessary.

    Args:
        directory_path: Directory path to ensure exists

    Raises:
        PermissionError: If directory cannot be created
    """
    with ErrorContext(logger, f"create directory {directory_path}"):
        directory_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Ensured directory exists: {directory_path}")


def discard_directory(directory_path: Path) -> bool:
    """Remove a directory without ever exposing it partially deleted.

    ``rename`` is atomic within a filesystem so this routine first renames dir
    and then deletes it.

    Args:
        directory_path: Directory to remove. A path that does not exist is
            treated as already discarded.

    Returns:
        True when nothing remains at ``directory_path``. False when the tree
        could not be moved or deleted and is still in place.
    """
    if not directory_path.exists():
        return True

    suffix = uuid.uuid4().hex[:8]
    staged = directory_path.with_name(f"{DISCARDING_PREFIX}{directory_path.name}-{suffix}")
    try:
        logger.info(f"Renaming {directory_path} to {staged} for deletion")
        directory_path.rename(staged)
    except OSError as e:
        # Detaching failed, so the atomic guarantee is off the table. Delete in
        # place anyway.
        logger.warning(f"Could not stage {directory_path} for deletion ({e}); deleting in place")
        shutil.rmtree(directory_path, ignore_errors=True)
        return not directory_path.exists()

    logger.info(f"Deleting {staged} ...")
    shutil.rmtree(staged, ignore_errors=True)
    if staged.exists():
        # The caller's path is already gone, so readers are safe and this
        # counts as success. Only the detached copy lingers, holding disk.
        logger.error(f"Discarded {directory_path}, but staged copy {staged} remains")
    return True


def safe_read_file(file_path: Path, encoding: str = "utf-8") -> str:
    """
    Safely read file content with error handling.

    Args:
        file_path: Path to file to read
        encoding: File encoding (default: utf-8)

    Returns:
        File content as string

    Raises:
        FileNotFoundError: If file doesn't exist
        PermissionError: If file is not readable
        UnicodeDecodeError: If file encoding is invalid
    """
    with ErrorContext(logger, f"read file {file_path}"):
        try:
            with open(file_path, "r", encoding=encoding) as file:
                content = file.read()
            logger.debug(f"Read file: {file_path} ({len(content)} characters)")
            return content
        except UnicodeDecodeError:
            # Try with different encodings
            for fallback_encoding in ["latin-1", "cp1252"]:
                try:
                    with open(file_path, "r", encoding=fallback_encoding) as file:
                        content = file.read()
                    logger.warning(f"Read file with {fallback_encoding} encoding: {file_path}")
                    return content
                except UnicodeDecodeError:
                    continue
            raise UnicodeDecodeError(
                f"Could not decode file with any supported encoding: {file_path}"
            )


def safe_write_file(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    Safely write content to file with error handling.

    Args:
        file_path: Path to file to write
        content: Content to write
        encoding: File encoding (default: utf-8)

    Raises:
        PermissionError: If file is not writable
    """
    with ErrorContext(logger, f"write file {file_path}"):
        # Ensure parent directory exists
        ensure_directory_exists(file_path.parent)

        with open(file_path, "w", encoding=encoding) as file:
            file.write(content)

        logger.debug(f"Wrote file: {file_path} ({len(content)} characters)")


def get_file_info(file_path: Path) -> Dict[str, Any]:
    """
    Get file information and metadata.

    Args:
        file_path: Path to file

    Returns:
        Dictionary containing file information
    """
    with ErrorContext(logger, f"get file info {file_path}"):
        stat = file_path.stat()

        return {
            "path": str(file_path),
            "name": file_path.name,
            "size_bytes": stat.st_size,
            "size_mb": stat.st_size / (1024 * 1024),
            "modified_time": stat.st_mtime,
            "is_file": file_path.is_file(),
            "is_directory": file_path.is_dir(),
            "extension": file_path.suffix,
            "parent": str(file_path.parent),
        }


def find_project_root(start_path: Path, markers: Optional[List[str]] = None) -> Optional[Path]:
    """
    Find project root directory by looking for marker files.

    Args:
        start_path: Starting path to search from
        markers: List of marker files/directories to look for

    Returns:
        Project root path or None if not found
    """
    if markers is None:
        markers = [".git", "pyproject.toml", "setup.py", "package.json", "Cargo.toml"]

    current_path = start_path.resolve()

    while current_path != current_path.parent:
        for marker in markers:
            if (current_path / marker).exists():
                logger.debug(f"Found project root at {current_path} (marker: {marker})")
                return current_path
        current_path = current_path.parent

    logger.debug("No project root found")
    return None
