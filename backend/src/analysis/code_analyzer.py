"""
Code Analysis Engine for the Code Knowledge Base.

This module orchestrates the complete code analysis process,
from directory scanning to component extraction and catalog population.
"""

from pathlib import Path
from typing import Iterable, List, Dict, Any
from datetime import datetime, timezone

from ..core.interfaces import IComponentRepository
from ..core.models import (
    Component,
    KnowledgeBase,
    ParseFailureReport,
    ParseResult,
    ComponentType,
    IdScheme,
    DEFAULT_SCHEME,
    qualify,
)
from ..core.file_utils import FileScanner
from ..core.tree_sitter_wrapper import TreeSitterWrapper, tree_cache_key
from ..core.logging_utils import get_logger, ErrorContext, AnalysisError
from .dart_libraries import DartLibraryQualifier
from .ruby_app_boundaries import RubyAppBoundaryQualifier


logger = get_logger(__name__)


PARSE_FAILURE_REPORTS_KEY = "parse_failure_reports"
PARSE_FAILURE_SUMMARY_KEY = "parse_failure_summary"
DART_UNRESOLVED_PARTS_KEY = "dart_unresolved_parts"


def collect_parse_failure_metadata(
    reports: Iterable[ParseFailureReport],
    *,
    scope: str = "full-run",
) -> Dict[str, Any]:
    per_language: Dict[str, Dict[str, int]] = {}
    detail: List[Dict[str, Any]] = []
    # `untrusted_name_count` is summed here because `is_structurally_clean`
    # factors it into the defect tally: without it a file whose only defect is a
    # refused name reports as defective with every tracked count at zero.
    totals = {
        "files_reported": 0,
        "structurally_clean_files": 0,
        "files_with_defects": 0,
        "parse_error_count": 0,
        "error_node_count": 0,
        "missing_node_count": 0,
        "untrusted_name_count": 0,
    }

    for report in reports:
        clean = report.is_structurally_clean()
        language_totals = per_language.setdefault(
            report.language,
            {
                "files_reported": 0,
                "structurally_clean_files": 0,
                "files_with_defects": 0,
                "parse_error_count": 0,
                "error_node_count": 0,
                "missing_node_count": 0,
                "untrusted_name_count": 0,
            },
        )
        for bucket in (totals, language_totals):
            bucket["files_reported"] += 1
            bucket["structurally_clean_files"] += 1 if clean else 0
            bucket["files_with_defects"] += 0 if clean else 1
            bucket["parse_error_count"] += report.parse_error_count
            bucket["error_node_count"] += report.error_node_count
            bucket["missing_node_count"] += report.missing_node_count
            bucket["untrusted_name_count"] += report.untrusted_name_count

        if not clean:
            detail.append(report.to_dict())

    summary: Dict[str, Any] = {"scope": scope}
    summary.update(totals)
    summary["by_language"] = per_language

    return {
        PARSE_FAILURE_REPORTS_KEY: detail,
        PARSE_FAILURE_SUMMARY_KEY: summary,
    }


def _relative_posix(file_path: Path, root: Path) -> str:
    """The path as git-diff names it, or as given when outside root."""
    try:
        return file_path.relative_to(root).as_posix()
    except ValueError:
        return str(file_path)


def _repo_relative_reports(
    results: Iterable[ParseResult], source_directory: Path
) -> List[ParseFailureReport]:
    """Defect reports keyed the way the refresh path keys its own: repo-relative.

    Analysis scans absolute paths, but the refresh supersedes a prior row by
    matching it against git-diff paths. An absolute row can never match one, so a
    file defective at index time would keep its row through every later refresh.
    """
    reports = []
    for result in results:
        report = result.failure_report()
        try:
            report.file_path = report.file_path.relative_to(source_directory)
        except ValueError:
            # Outside the scanned root (a symlink out of the tree); leave it as
            # given rather than inventing a relative form for it.
            pass
        reports.append(report)
    return reports


def merge_parse_failure_metadata(
    prior_metadata: Dict[str, Any],
    run_reports: Iterable[ParseFailureReport],
    superseded_files: Iterable[str],
) -> Dict[str, Any]:
    superseded = set(superseded_files)
    retained: List[ParseFailureReport] = []
    for row in prior_metadata.get(PARSE_FAILURE_REPORTS_KEY, []) or []:
        try:
            if str(row.get("file_path", "")) in superseded:
                continue
            retained.append(ParseFailureReport.from_dict(row))
        except Exception:
            logger.warning("Skipping unreadable prior parse-failure report row")

    return collect_parse_failure_metadata(list(run_reports) + retained, scope="changed-files")


class CodeAnalyzer:
    """
    Main code analysis engine orchestrating the analysis process.

    Coordinates directory scanning, file parsing, component extraction,
    and knowledge base population.
    """

    def __init__(
        self,
        component_repository: IComponentRepository,
        exclude_patterns: List[str] | None = None,
    ):
        """
        Initialize code analyzer.

        Args:
            component_repository: Repository for storing discovered components
            exclude_patterns: Exclusion patterns for the scanner; None uses the config's.
        """
        self._component_repository = component_repository
        self._file_scanner = FileScanner(exclude_patterns=exclude_patterns)
        self._tree_sitter_wrapper = TreeSitterWrapper()
        # Id scheme for this analyzer's runs; recorded on the produced KB. New
        # indexes use DEFAULT_SCHEME; refresh overrides via configure_id_context
        # with the scheme resolved from the space's persisted knowledge base.
        self._id_scheme = DEFAULT_SCHEME
        # Parsed ASTs retained from the per-file parse, keyed by tree_cache_key,
        # to hand to FlowAnalyzer so it need not re-parse. Scoped to one
        # analyze_directory run; drained via take_parsed_trees().
        self._parsed_trees: Dict[str, Any] = {}
        # Per-file parse reports from the last analyze_directory run (take_failure_reports()).
        self._failure_reports: List[ParseFailureReport] = []
        # Repo-relative files the last run could not read or parse, so it left them out.
        self._unparsed_files: List[str] = []
        # Keep the scanner's notion of "analyzable" in sync with the parsers
        # that actually loaded, so files in configured-but-unsupported
        # languages (e.g. C/C++) are reported as skipped instead of silently
        # failing to parse.
        self._file_scanner.set_available_languages(
            self._tree_sitter_wrapper.get_supported_languages()
        )

    def analyze_directory(
        self,
        directory_path: Path,
        space_id: str,
        id_scheme: IdScheme = DEFAULT_SCHEME,
        files: List[Path] | None = None,
        dart_qualifier: DartLibraryQualifier | None = None,
    ) -> KnowledgeBase:
        """
        Analyze a complete directory structure.

        Args:
            directory_path: Root directory to analyze
            space_id: Space identifier (required)
            id_scheme: Component-id scheme to index under. Defaults to the
                current scheme for new spaces; callers may pass a specific scheme
                (e.g. to reindex a space under its original scheme).
            files: Analyze only these files (absolute paths) instead of scanning;
                refresh passes its changed files.
            dart_qualifier: With ``files``, the Dart qualifier to apply. Changed files alone
                can't resolve ``part`` directives, so refresh builds it over the whole clone.

        Returns:
            KnowledgeBase containing all discovered components

        Raises:
            AnalysisError: If analysis fails
        """
        with ErrorContext(logger, f"analyze directory {directory_path}"):
            logger.info(f"Starting code analysis of directory: {directory_path}")

            # Clear existing components
            self._component_repository.clear()
            # Fresh tree cache for this run (drained by take_parsed_trees()).
            self._parsed_trees = {}
            self._failure_reports = []
            self._unparsed_files = []

            # Index under the requested scheme, rooted at the analyzed directory
            # so V2 path-hashed prefixes are repo-relative.
            self.configure_id_context(directory_path, id_scheme)

            # Scan for source files
            if files is None:
                source_files = self._file_scanner.scan_directory(directory_path)
            else:
                # The scan's unsupported-language and size-cap skips.
                source_files = [f for f in files if self._file_scanner.should_include_file(f)]

            if not source_files:
                logger.warning("No source files found in directory")
                return self._create_empty_knowledge_base(directory_path, space_id)

            # Ruby only: when the repo contains multiple applications (several
            # Gemfile/*.gemspec roots), qualify nested-app component IDs by app
            # root so same-named top-level constants from different apps don't
            # overwrite each other. Must run before add_component — the catalog
            # collision is last-write-wins at insert time. Single-app repos are
            # untouched; see ruby_app_boundaries.py for the semantic rationale.
            #
            # Gated on having at least one Ruby file so non-Ruby repos don't pay
            # for a second directory traversal (the qualifier scans for Gemfiles).
            _RUBY_EXTENSIONS = {".rb", ".rake", ".gemspec"}
            has_ruby_files = any(f.suffix in _RUBY_EXTENSIONS for f in source_files)
            ruby_qualifier = (
                RubyAppBoundaryQualifier(
                    directory_path,
                    is_excluded_directory=self._file_scanner._is_excluded_directory,
                )
                if has_ruby_files
                else None
            )

            # Handed the `.dart` subset of the scan's own selection rather than
            # a directory to re-walk: membership of the analysed set is what
            # decides whether a `part` directive resolves.
            _DART_EXTENSIONS = {".dart"}
            if files is None:
                dart_files = [f for f in source_files if f.suffix.lower() in _DART_EXTENSIONS]
                has_dart_files = bool(dart_files)
                dart_qualifier = (
                    DartLibraryQualifier(directory_path, dart_files) if has_dart_files else None
                )

            logger.info(f"Analyzing {len(source_files)} source files")

            analysis_results = []
            successful_files = 0
            failed_files = 0
            total_lines = 0

            for file_path in source_files:
                try:
                    result = self._analyze_file(file_path)

                    # Retain the parsed tree for FlowAnalyzer to reuse (avoids a
                    # second read+parse of every file), then detach it from the
                    # ParseResult so the KnowledgeBase still holds no trees. The
                    # tree stays alive only via _parsed_trees, whose lifetime is
                    # scoped to the extract->resolve window (drained right after
                    # build_call_graph). Accumulating trees on the retained
                    # ParseResults instead would make peak memory scale with repo
                    # size, which is why they are detached here.
                    if result.syntax_tree is not None:
                        self._parsed_trees[
                            tree_cache_key(result.file_path, result.language)
                        ] = result.syntax_tree
                    result.syntax_tree = None

                    analysis_results.append(result)
                    total_lines += result.metadata.get("line_count", 0)

                    if result.has_errors():
                        failed_files += 1
                        logger.warning(
                            f"Parsing errors in {file_path}: {len(result.parse_errors)} errors"
                        )
                    else:
                        successful_files += 1

                    # Add components to repository
                    if ruby_qualifier is not None:
                        ruby_qualifier.qualify(result.components)
                    if dart_qualifier is not None:
                        dart_qualifier.qualify(result.components)
                    # Disambiguate overloaded functions/methods within this file
                    # before insert — two overloads share an id otherwise and the
                    # catalog is last-write-wins. Runs after the ruby/dart
                    # qualifiers (which finalize ids) and after any parser-level
                    # receiver qualification.
                    #
                    # V2 only: the id changes here, so legacy V1 spaces are left
                    # untouched to keep delta-refresh ids matching the persisted
                    # ones. They adopt this on a deliberate full V2 reindex.
                    if id_scheme == IdScheme.V2_PATH:
                        self._disambiguate_overloads(result.components)
                    for component in result.components:
                        self._component_repository.add_component(component)

                except Exception as e:
                    # A single unparseable file must not abort the run, but it
                    # must never pass silently either: the traceback is the only
                    # record, since the file yields no ParseResult to carry
                    # parse_errors into the knowledge base metadata.
                    failed_files += 1
                    self._unparsed_files.append(_relative_posix(file_path, directory_path))
                    logger.error(f"Failed to analyze file {file_path}: {e}", exc_info=True)

            logger.info(
                f"File analysis complete: {successful_files} successful, {failed_files} failed"
            )

            # Create knowledge base
            knowledge_base = self._create_knowledge_base(directory_path, analysis_results, space_id)

            # Part relationships that could not be grouped travel the way the
            # parse-failure reports do: as plain data in the run's metadata, so a
            # reader sees what was not grouped without re-reading the sources.
            if dart_qualifier is not None:
                knowledge_base.analysis_metadata[DART_UNRESOLVED_PARTS_KEY] = [
                    record.to_dict() for record in dart_qualifier.unresolved_records
                ]

            logger.info(
                f"Analysis complete: {knowledge_base.get_component_count()} components discovered"
            )
            return knowledge_base

    def take_parsed_trees(self) -> Dict[str, Any]:
        """Hand over the parsed ASTs from the last analyze_directory run.

        Transfers ownership to the caller (typically FlowAnalyzer via the
        orchestrator) and clears this analyzer's reference so the trees are not
        retained here. Keyed by ``tree_cache_key`` to match FlowAnalyzer's cache.
        """
        trees, self._parsed_trees = self._parsed_trees, {}
        return trees

    def configure_id_context(self, root: Path, scheme: IdScheme) -> None:
        """Set the root and id scheme used to generate component ids.

        Full indexing uses DEFAULT_SCHEME; space refresh calls this with the
        scheme resolved from the space's persisted knowledge base so regenerated
        ids match the persisted ones and the merge stays stable.
        """
        self._id_scheme = scheme
        self._tree_sitter_wrapper.set_id_context(root, scheme)

    def take_failure_reports(self) -> List[ParseFailureReport]:
        """Hand over the last analyze_directory run's per-file reports, clean ones included.

        Repo-relative paths. Refresh merges them into the prior KB's metadata.
        """
        reports, self._failure_reports = self._failure_reports, []
        return reports

    def analyze_file(self, file_path: Path) -> ParseResult:
        """
        Analyze a single source file.

        Args:
            file_path: Path to source file to analyze

        Returns:
            ParseResult containing extracted components

        Raises:
            AnalysisError: If file analysis fails
        """
        return self._analyze_file(file_path)

    def get_supported_languages(self) -> List[str]:
        """
        Get list of supported programming languages.

        Returns:
            List of supported language identifiers
        """
        return list(self._tree_sitter_wrapper.get_supported_languages())

    def get_analysis_statistics(self) -> Dict[str, Any]:
        """
        Get analysis statistics from the component repository.

        Returns:
            Dictionary containing analysis statistics
        """
        return self._component_repository.get_statistics()

    # Component kinds that can legitimately repeat a name in one scope (method
    # overloads, overloaded top-level/free functions). Not classes: renaming a
    # class would also change the ids of all its members.
    _OVERLOADABLE = frozenset(
        {ComponentType.METHOD, ComponentType.FUNCTION, ComponentType.CONSTRUCTOR}
    )

    def _disambiguate_overloads(self, components: List["Component"]) -> None:
        """Make overloaded functions/methods unique within one file in place.

        Two overloads (`Save(User)` / `Save(User,bool)`) build the same id, and
        the catalog keeps only the last — the rest lose their source, calls,
        description and vector. Here we append a short hash of the normalized
        signature to each colliding id and record the clean bare name in
        ``_name`` (so ``get_simple_name`` and call resolution still key on the
        real name). Non-colliding ids are untouched.

        A component nested in a renamed definition (a function or a variable
        inside the implementation of an ``@overload`` group) gets the new id as
        its parent, as the Ruby and Dart qualifiers do when they change ids.
        """
        import hashlib
        from collections import defaultdict

        groups: Dict[str, List["Component"]] = defaultdict(list)
        children_by_parent: Dict[str, List["Component"]] = defaultdict(list)
        for comp in components:
            if comp.type in self._OVERLOADABLE:
                groups[comp.id].append(comp)
            if comp.parent_component:
                children_by_parent[comp.parent_component].append(comp)

        for comp_id, group in groups.items():
            if len(group) < 2:
                continue
            # C/C++ prototype-vs-definition sharing an id is a deliberate merge
            # handled by the catalog; leave those alone.
            if all(c.language in ("c", "cpp") for c in group):
                continue

            used: set[str] = set()
            for comp in group:
                sig = " ".join((comp.attributes.signature or "").split())
                if sig:
                    disc = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:4]
                else:
                    disc = f"p{len(comp.attributes.parameters)}"  # arity fallback
                # Guarantee uniqueness even if two signatures normalize equal.
                unique = disc
                n = 1
                while unique in used:
                    n += 1
                    unique = f"{disc}{n}"
                used.add(unique)

                if not comp._name:
                    comp._name = comp.get_simple_name()
                comp.id = f"{comp.id}_{unique}"

            # The children still name the old id; each one belongs to the
            # member whose lines hold it.
            for child in children_by_parent.get(comp_id, ()):
                for member in group:
                    if member.line_number <= child.line_number <= member.end_line_number:
                        child.parent_component = member.id
                        break

    def _analyze_file(self, file_path: Path) -> ParseResult:
        """
        Internal method to analyze a single file.

        Args:
            file_path: Path to file to analyze

        Returns:
            ParseResult with extracted components
        """
        with ErrorContext(logger, f"analyze file {file_path}"):
            # Detect language
            language = self._file_scanner.detect_language(file_path)
            if not language:
                raise AnalysisError(f"Unsupported file type: {file_path}")

            # Check if language is supported by Tree-sitter
            if not self._tree_sitter_wrapper.is_language_supported(language):
                raise AnalysisError(f"No Tree-sitter parser available for language: {language}")

            # Parse file with Tree-sitter
            parse_result = self._tree_sitter_wrapper.parse_file(file_path, language)

            # Count lines in the source file
            try:
                line_count = file_path.read_text(errors="replace").count("\n") + 1
                parse_result.metadata["line_count"] = line_count
            except Exception:
                parse_result.metadata["line_count"] = 0

            logger.debug(
                f"Analyzed {file_path}: {len(parse_result.components)} components extracted"
            )
            return parse_result

    def _create_knowledge_base(
        self, source_directory: Path, analysis_results: List[ParseResult], space_id: str
    ) -> KnowledgeBase:
        """
        Create knowledge base from analysis results.

        Args:
            source_directory: Root directory that was analyzed
            analysis_results: List of file analysis results
            space_id: Space identifier

        Returns:
            Complete knowledge base
        """

        # Collect all components and add space_id prefix
        all_components = self._component_repository.get_all_components()
        for component in all_components:
            component.space_id = space_id
            component.id = qualify(space_id, component.id)
            if component.parent_component:
                component.parent_component = qualify(space_id, component.parent_component)

        # Create analysis metadata
        skipped_files = dict(getattr(self._file_scanner, "skipped_files", {}) or {})
        self._failure_reports = _repo_relative_reports(analysis_results, source_directory)
        metadata = {
            "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
            "source_directory": str(source_directory),
            "files_analyzed": len(analysis_results),
            "total_components": len(all_components),
            "total_lines": sum(r.metadata.get("line_count", 0) for r in analysis_results),
            "languages_detected": self._get_detected_languages(analysis_results),
            "analysis_errors": self._collect_analysis_errors(analysis_results),
            "supported_languages": self.get_supported_languages(),
            "skipped_files": skipped_files,
            "skipped_file_count": sum(skipped_files.values()),
            "unparsed_files": sorted(self._unparsed_files),
            **collect_parse_failure_metadata(self._failure_reports),
        }

        # Create knowledge base
        knowledge_base = KnowledgeBase(
            source_directory=source_directory,
            components=all_components,
            analysis_metadata=metadata,
            created_at=datetime.now(timezone.utc).isoformat(),
            version="1.0",
            space_id=space_id,
            id_scheme=self._id_scheme.value,
        )

        return knowledge_base

    def _create_empty_knowledge_base(self, source_directory: Path, space_id: str) -> KnowledgeBase:
        """Create empty knowledge base when no files are found."""
        skipped_files = dict(getattr(self._file_scanner, "skipped_files", {}) or {})
        metadata = {
            "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
            "source_directory": str(source_directory),
            "files_analyzed": 0,
            "total_components": 0,
            "languages_detected": [],
            "analysis_errors": [],
            "supported_languages": self.get_supported_languages(),
            "skipped_files": skipped_files,
            "skipped_file_count": sum(skipped_files.values()),
            **collect_parse_failure_metadata([]),
        }

        return KnowledgeBase(
            source_directory=source_directory,
            components=[],
            analysis_metadata=metadata,
            created_at=datetime.now(timezone.utc).isoformat(),
            version="1.0",
            space_id=space_id,
            id_scheme=self._id_scheme.value,
        )

    def _get_detected_languages(self, analysis_results: List[ParseResult]) -> List[str]:
        """Get list of detected programming languages."""
        languages = set()
        for result in analysis_results:
            languages.add(result.language)
        return list(languages)

    def _collect_analysis_errors(self, analysis_results: List[ParseResult]) -> List[Dict[str, Any]]:
        """Collect all analysis errors from results."""
        errors = []
        for result in analysis_results:
            for error in result.parse_errors:
                errors.append(
                    {"file": str(result.file_path), "language": result.language, "error": error}
                )
        return errors
