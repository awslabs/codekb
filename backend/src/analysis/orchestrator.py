"""Analysis orchestrator for coordinating analysis operations."""

import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Dict, Any

from ..catalog.component_catalog import ComponentCatalog, SimpleBusinessBehaviorAnalyzer
from ..flow.flow_analyzer import FlowAnalyzer
from .business_relevance_analyzer import BusinessRelevanceAnalyzer
from .repo_business_context_extractor import RepoBusinessContextExtractor
from ..persistence.knowledge_base_manager import KnowledgeBaseManager
from ..core.models import KnowledgeBase, ComponentType
from ..core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class AnalysisContext:
    """Container for analysis components and results."""

    knowledge_base: KnowledgeBase
    catalog: ComponentCatalog
    flow_analyzer: FlowAnalyzer
    statistics: Dict[str, Any]
    # Set by CodeKBService when cached: the knowledge_base.json ETag this was built
    # from, and when it was last checked against S3 (the version-check clock).
    etag: str | None = None
    checked_at: float = 0.0


class AnalysisOrchestrator:
    """Orchestrates analysis operations and component initialization."""

    def __init__(self, kb_manager: KnowledgeBaseManager):
        """Initialize orchestrator with knowledge base manager."""
        self.kb_manager = kb_manager

    def get_supported_languages(self) -> list[str]:
        """Return the list of programming languages CodeKB can analyze."""
        from .code_analyzer import CodeAnalyzer

        catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
        return CodeAnalyzer(catalog).get_supported_languages()

    def load_space(self, space_id: str) -> AnalysisContext:
        """Load existing space and initialize all components.

        Args:
            space_id: Space identifier

        Returns:
            AnalysisContext with initialized components

        Raises:
            FileNotFoundError: If no knowledge base exists for the space.
            Exception: For any other load failure.
        """
        knowledge_base = self.kb_manager.import_knowledge_base(space_id)
        if knowledge_base is None:
            # KnowledgeBaseManager.import_knowledge_base returns None for a
            # missing space rather than raising. Convert that to a real
            # exception so callers (especially analyze_space) can distinguish
            # "not there yet" from a real failure.
            raise FileNotFoundError(f"No knowledge base found for space '{space_id}'")

        return self._initialize_context(knowledge_base)

    @staticmethod
    def _rebase_component_paths(
        knowledge_base: KnowledgeBase, *, from_root: Path, to_root: Path
    ) -> None:
        """Move component paths from one absolute root to another, in place.

        Analysis runs in a disposable working tree, but a component path has to name
        the durable location the source is served from — otherwise it is a dead
        pointer the moment the pipeline deletes its working tree. The two differ only
        in their root, so persistence swaps one for the other.

        A path not under ``from_root`` is left alone: there is no correct rebase for
        it, and leaving it is no worse than before.

        Naturally a no-op when the two roots are equal, which is the case for
        callers whose analysed directory is already the durable one.

        Args:
            knowledge_base: Knowledge base whose components are rewritten in place.
            from_root: Root the paths currently sit under.
            to_root: Root to move them to.
        """
        unrelated: list[Path] = []
        for component in knowledge_base.components:
            try:
                relative = component.file_path.relative_to(from_root)
            except ValueError:
                unrelated.append(component.file_path)
                continue
            component.file_path = to_root / relative

        if unrelated:
            # Counted and sampled rather than logged per component: a knowledge
            # base can hold tens of thousands of these.
            logger.warning(
                f"{len(unrelated)} component path(s) are not under {from_root}, "
                f"so they were left unrebased (e.g. {unrelated[0]})"
            )

    def analyze_space(
        self,
        directory: Path,
        space_id: str,
        business_behavior_enabled: bool = False,
        on_business_context_status: Callable[[str, dict[str, Any] | None], None] | None = None,
        persisted_source_root: Path | None = None,
    ) -> AnalysisContext:
        """Analyze new space and initialize all components.

        Args:
            directory: Directory to analyze
            space_id: Space identifier
            business_behavior_enabled: Master switch for the business-behavior
                feature. When False (the default), no business behavior is
                computed or persisted: the heuristic analyzer is not wired, the
                repo business-context LLM call is skipped, and relevance scoring
                does not run. Each component's ``business_behavior`` stays empty
                and is omitted from the stored knowledge base. When True, all
                three run, exactly as before.
            on_business_context_status: Optional callback invoked with "in_progress",
                "completed", or "failed" while extracting business context
            persisted_source_root: Durable root to store paths against — both the
                knowledge base's ``source_directory`` and every component path.
                Defaults to ``directory``, which is only right when the analysed
                directory outlives the run. Callers whose analysed directory is a
                disposable working tree should pass the location the source is
                served from instead.

        Returns:
            AnalysisContext with initialized components

        Raises:
            ValueError: If directory is invalid
            Exception: If analysis fails
        """
        # Validate directory
        if not directory.exists():
            raise ValueError(f"Directory '{directory}' does not exist")
        if not directory.is_dir():
            raise ValueError(f"'{directory}' is not a directory")

        # Check for existing knowledge base
        try:
            return self.load_space(space_id)
        except Exception as e:
            # Expected the first time a space is analyzed; keep at DEBUG so
            # customer-issue investigators can still see the miss.
            logger.debug(f"No existing KB for space '{space_id}', will analyze from scratch: {e}")

        # Perform fresh analysis
        from .code_analyzer import CodeAnalyzer

        # Only wire the heuristic business-behavior analyzer when the feature is
        # on. With no analyzer, ComponentCatalog leaves business_behavior empty.
        catalog = ComponentCatalog(
            SimpleBusinessBehaviorAnalyzer() if business_behavior_enabled else None
        )
        analyzer = CodeAnalyzer(catalog)

        logger.info(f"[analyze_space] Analyzing directory: {directory}")
        t0 = time.perf_counter()
        knowledge_base = analyzer.analyze_directory(directory, space_id)
        logger.info(
            f"[analyze_space] analyze_directory done in {time.perf_counter() - t0:.2f}s — "
            f"components={knowledge_base.get_component_count()}"
        )

        # Build call graph
        flow_analyzer = FlowAnalyzer(catalog)
        logger.info("[analyze_space] Building call graph...")
        t0 = time.perf_counter()
        # Reuse the trees CodeAnalyzer already parsed, so build_call_graph does
        # not re-read and re-parse every file. take_parsed_trees() clears the
        # analyzer's reference; build_call_graph clears its cache when done.
        flow_analyzer.build_call_graph(
            knowledge_base.components, tree_cache=analyzer.take_parsed_trees()
        )
        logger.info(
            f"[analyze_space] build_call_graph done in {time.perf_counter() - t0:.2f}s — "
            f"entries={len(flow_analyzer._call_graph)}"
        )
        knowledge_base.call_graph = flow_analyzer._call_graph

        business_context = RepoBusinessContextExtractor.empty_context("disabled")
        if business_behavior_enabled:
            logger.info("[analyze_space] Extracting repository business context...")
            if on_business_context_status:
                on_business_context_status("in_progress", None)
            t0 = time.perf_counter()
            extractor = RepoBusinessContextExtractor()
            business_context = extractor.extract(
                directory, knowledge_base.components, knowledge_base.call_graph
            )
            # Folded into the space_creation token record by the service.
            knowledge_base.analysis_metadata["business_context_usage"] = extractor.usage_part()
            if on_business_context_status:
                on_business_context_status("completed", business_context)
            logger.info(
                f"[analyze_space] business context extraction done in "
                f"{time.perf_counter() - t0:.2f}s"
            )
        knowledge_base.analysis_metadata["business_context"] = business_context

        # Score callable components after the call graph exists, while the
        # catalog and knowledge base still share the same Component objects.
        # Skipped entirely when the business-behavior feature is off, so the
        # relevance fields stay at their defaults and the block is not persisted.
        if business_behavior_enabled:
            logger.info("[analyze_space] Scoring business relevance...")
            t0 = time.perf_counter()
            BusinessRelevanceAnalyzer(business_context).score_components(
                knowledge_base.components, knowledge_base.call_graph
            )
            logger.info(
                f"[analyze_space] business relevance scoring done in "
                f"{time.perf_counter() - t0:.2f}s"
            )

        # Compute per-component source hashes so space refresh can do
        # method-level change detection from the very first refresh.
        from .content_hash import backfill_component_hashes

        hashed = backfill_component_hashes(directory, knowledge_base.components)
        logger.info(f"[analyze_space] computed content hashes for {hashed} component(s)")

        # Save knowledge base.
        #
        # What gets stored is the durable source location, not the working tree that
        # was analysed: `directory` is deleted when the pipeline ends, so a path under
        # it is dead on arrival.
        #
        # The two kinds of stored path are handled differently, on purpose:
        #   - `source_directory` (and its analysis_metadata copy) is set to the durable
        #     root and left there. The exported KB and the finalize step both must
        #     record the durable location, so this is deliberately not reverted.
        #   - Every component path is moved onto the durable root only for the export,
        #     then moved back in the `finally`, because the context returned below —
        #     and the summarization step that runs on it — open these files from the
        #     tree still on disk.
        #
        # The asymmetry is safe because fresh-analysis component paths are absolute:
        # in-memory readers open them directly and never resolve a relative path
        # against `source_directory`, so its pointing at the not-yet-populated durable
        # root during this window is inert.
        persisted_root = persisted_source_root or directory
        knowledge_base.source_directory = persisted_root
        knowledge_base.analysis_metadata["source_directory"] = str(persisted_root)
        self._rebase_component_paths(knowledge_base, from_root=directory, to_root=persisted_root)
        try:
            logger.info("[analyze_space] Exporting knowledge base to S3...")
            t0 = time.perf_counter()
            self.kb_manager.export_knowledge_base(knowledge_base)
            logger.info(
                f"[analyze_space] export_knowledge_base done in {time.perf_counter() - t0:.2f}s"
            )
        finally:
            self._rebase_component_paths(
                knowledge_base, from_root=persisted_root, to_root=directory
            )

        # Initialize context
        context = self._initialize_context(knowledge_base)
        context.statistics["directory"] = str(directory)

        return context

    def auto_load_or_analyze(self, directory: Path, space_id: str) -> AnalysisContext:
        """Smart initialization: load if exists, otherwise analyze.

        Args:
            directory: Directory to analyze
            space_id: Space identifier

        Returns:
            AnalysisContext with initialized components

        Raises:
            ValueError: If directory is invalid
            Exception: If both load and analyze fail
        """
        # Validate directory
        if not directory.exists():
            raise ValueError(f"Directory '{directory}' does not exist")

        # Try loading first
        try:
            return self.load_space(space_id)
        except Exception:
            # Fall back to analysis
            return self.analyze_space(directory, space_id)

    def _initialize_context(
        self, knowledge_base: KnowledgeBase, *, business_behavior_enabled: bool = True
    ) -> AnalysisContext:
        """Initialize analysis context from knowledge base.
        Args:
            knowledge_base: Loaded knowledge base
            business_behavior_enabled: Wire the heuristic analyzer, which fills
                empty descriptions on the KB's own Component objects in place.

        Returns:
            AnalysisContext with all components initialized
        """
        component_count = len(knowledge_base.components)
        logger.info(
            f"[init_context] Start — components={component_count}, "
            f"call_graph_entries={len(knowledge_base.call_graph) if knowledge_base.call_graph else 0}"
        )

        # Rebuild catalog
        logger.info("[init_context] Rebuilding catalog...")
        t0 = time.perf_counter()
        catalog = ComponentCatalog(
            SimpleBusinessBehaviorAnalyzer() if business_behavior_enabled else None
        )
        for component in knowledge_base.components:
            catalog.add_component(component)
        logger.info(f"[init_context] Catalog rebuilt in {time.perf_counter() - t0:.2f}s")

        # Rebuild flow analyzer
        logger.info("[init_context] Rebuilding flow analyzer...")
        t0 = time.perf_counter()
        flow_analyzer = FlowAnalyzer(catalog)
        if hasattr(knowledge_base, "call_graph") and knowledge_base.call_graph:
            flow_analyzer._call_graph = self._reconstruct_call_graph(knowledge_base.call_graph)
            # Derive reverse call graph from the forward graph in O(V + E).
            # The persisted KB only stores the forward graph; without this,
            # dependents lookups (analyze_dependencies) return empty and the
            # connected-components BFS below would have to scan the entire
            # forward graph for every visited node.
            for caller, calls in flow_analyzer._call_graph.items():
                for call in calls:
                    callee = call[0] if isinstance(call, (tuple, list)) else call
                    flow_analyzer._reverse_call_graph.setdefault(callee, []).append(caller)
            flow_analyzer._call_graph_built = True
        else:
            flow_analyzer._call_graph = {}
        logger.info(f"[init_context] Flow analyzer rebuilt in {time.perf_counter() - t0:.2f}s")

        # Get statistics
        logger.info("[init_context] Collecting catalog statistics...")
        t0 = time.perf_counter()
        statistics = catalog.get_statistics()
        statistics["call_graph_entries"] = sum(
            len(calls) for calls in flow_analyzer._call_graph.values()
        )
        statistics["total_lines"] = knowledge_base.analysis_metadata.get("total_lines", 0)

        # Files skipped because CodeKB has no parser for their language, keyed by
        # language name (see analysis.unsupported_languages in default.yaml).
        # Carried through to status.json so the UI can tell users that part of
        # their codebase wasn't analyzed. Mirrors total_lines above.
        statistics["skipped_files"] = knowledge_base.analysis_metadata.get("skipped_files", {})
        statistics["skipped_file_count"] = knowledge_base.analysis_metadata.get(
            "skipped_file_count", 0
        )

        # Business relevance tier distribution (functions and methods only)
        tier_counts: dict[str, int] = {}
        for comp in knowledge_base.components:
            if comp.type not in (ComponentType.FUNCTION, ComponentType.METHOD):
                continue
            tier = comp.business_behavior.business_relevance_tier
            if tier:
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
        statistics["business_relevance_tiers"] = tier_counts
        statistics["business_relevance_function_method_tiers"] = tier_counts
        statistics["business_relevance_function_method_count"] = sum(tier_counts.values())

        logger.info(
            f"[init_context] Catalog statistics collected in {time.perf_counter() - t0:.2f}s"
        )

        logger.info("[init_context] Done")
        return AnalysisContext(
            knowledge_base=knowledge_base,
            catalog=catalog,
            flow_analyzer=flow_analyzer,
            statistics=statistics,
        )

    def _reconstruct_call_graph(self, call_graph_data: Dict) -> Dict:
        """Reconstruct call graph from serialized format.

        Args:
            call_graph_data: Serialized call graph

        Returns:
            Reconstructed call graph
        """
        reconstructed = {}
        for comp_id, calls in call_graph_data.items():
            reconstructed[comp_id] = [
                tuple(call) if isinstance(call, list) else call for call in calls
            ]
        return reconstructed
