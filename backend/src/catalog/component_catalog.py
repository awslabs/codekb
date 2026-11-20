"""
Component Catalog implementation for the Code Knowledge Base.

This module provides the core component repository functionality,
including storage, retrieval, search, and business behavior analysis.
"""

from typing import List, Optional, Dict, Any, Set
from pathlib import Path

from ..core.interfaces import IComponentRepository, IBusinessBehaviorAnalyzer
from ..core.models import Component, ComponentType, BusinessBehavior
from ..core.logging_utils import get_logger, ErrorContext


logger = get_logger(__name__)

# Upper bound on the length of a component search pattern. Matching is linear in
# the pattern length (see ComponentCatalog._matches_segments), so this is a
# sanity bound on wasted work rather than a security control — no component name
# search needs to be this long.
MAX_SEARCH_PATTERN_LENGTH = 200


class ComponentCatalog(IComponentRepository):
    """
    Component repository implementation providing storage and search functionality.

    Manages the complete catalog of discovered components with indexing
    for efficient search and retrieval operations.
    """

    def __init__(self, business_analyzer: Optional[IBusinessBehaviorAnalyzer] = None):
        """
        Initialize component catalog.

        Args:
            business_analyzer: Optional business behavior analyzer
        """
        self._components: Dict[str, Component] = {}
        self._name_index: Dict[str, Set[str]] = {}  # name -> component_ids
        self._type_index: Dict[ComponentType, Set[str]] = {}  # type -> component_ids
        self._file_index: Dict[Path, Set[str]] = {}  # file -> component_ids
        self._business_analyzer = business_analyzer
        # Number of components silently replaced by a later component with the
        # same id (id collisions / data loss). Surfaced in statistics so these
        # are observable instead of only a log line.
        self._overwritten_count = 0

    def add_component(self, component: Component) -> None:
        """
        Add a component to the catalog.

        Args:
            component: The component to add

        Raises:
            ValueError: If component is invalid or already exists
        """
        with ErrorContext(logger, f"add component {component.id}"):
            if not component.id:
                raise ValueError("Component ID cannot be empty")

            if component.id in self._components:
                existing = self._components[component.id]
                # C/C++ only: the same entity can appear as a bodyless
                # prototype (header) and a full definition (source). Keep
                # whichever spans more lines (the definition); on a tie we keep
                # the existing one. This makes the common .h/.cpp merge
                # deterministic for strictly-different sizes (a prototype vs its
                # definition never tie in practice) — ties remain order-
                # dependent but harmless. Gated on language, so no other
                # language's behavior changes, and this normal merge is logged
                # at debug (not warning) since it's expected on every pair.
                if component.language in ("cpp", "c") and existing.language in (
                    "cpp",
                    "c",
                ):
                    new_lines = component.attributes.line_count if component.attributes else 0
                    old_lines = existing.attributes.line_count if existing.attributes else 0
                    if new_lines <= old_lines:
                        logger.debug(
                            "C/C++ merge: keeping existing definition over a "
                            f"smaller/equal declaration for {component.id}"
                        )
                        return
                    logger.debug(
                        f"C/C++ merge: definition supersedes earlier declaration for {component.id}"
                    )
                else:
                    self._overwritten_count += 1
                    logger.warning(f"Component already exists, updating: {component.id}")

            # Analyze business behavior if analyzer is available
            if self._business_analyzer and not component.business_behavior.description:
                try:
                    component.business_behavior = self._business_analyzer.analyze_business_behavior(
                        component
                    )
                except Exception as e:
                    logger.warning(f"Business behavior analysis failed for {component.id}: {e}")

            # Store component
            self._components[component.id] = component

            # Update indexes
            self._update_indexes(component)

            logger.debug(f"Added component: {component.id} ({component.type.value})")

    def find_component(self, name: str) -> Optional[Component]:
        """
        Find a component by exact name match.

        Args:
            name: Exact component name to find

        Returns:
            Component if found, None otherwise
        """
        component_ids = self._name_index.get(name, set())

        if not component_ids:
            return None

        # If multiple components have the same name, return the first one
        # In practice, this should be rare due to unique IDs
        component_id = next(iter(component_ids))
        return self._components.get(component_id)

    def search_components(self, pattern: str) -> List[Component]:
        """
        Search components by name pattern.

        Args:
            pattern: Search pattern (supports '*' wildcards)

        Returns:
            List of matching components, ranked by relevance.
            Empty list if the pattern exceeds MAX_SEARCH_PATTERN_LENGTH.
        """
        if len(pattern) > MAX_SEARCH_PATTERN_LENGTH:
            logger.warning(
                f"Search pattern rejected: {len(pattern)} characters exceeds the "
                f"{MAX_SEARCH_PATTERN_LENGTH}-character limit"
            )
            return []

        # Split once, then match every component against the segment list.
        segments = self._pattern_segments(pattern)

        matching_components: List[Component] = [
            component
            for component in self._components.values()
            if self._matches_segments(segments, component.name)
        ]

        # Rank results by relevance (exact matches first, then by name length)
        matching_components.sort(
            key=lambda c: (
                c.name.lower() != pattern.lower(),  # Exact matches first
                len(c.name),  # Shorter names first
                c.name.lower(),  # Alphabetical order
            )
        )

        logger.debug(f"Search '{pattern}' found {len(matching_components)} components")
        return matching_components

    def get_all_components(self) -> List[Component]:
        """
        Get all components in the catalog.

        Returns:
            List of all stored components
        """
        return list(self._components.values())

    def get_components_by_type(self, component_type: str) -> List[Component]:
        """
        Get components filtered by type.

        Args:
            component_type: Type to filter by

        Returns:
            List of components of the specified type
        """
        try:
            comp_type = ComponentType(component_type.lower())
        except ValueError:
            logger.warning(f"Invalid component type: {component_type}")
            return []

        component_ids = self._type_index.get(comp_type, set())
        return [
            self._components[comp_id] for comp_id in component_ids if comp_id in self._components
        ]

    def get_components_by_file(self, file_path: Path) -> List[Component]:
        """
        Get components from a specific file.

        Args:
            file_path: File path to filter by

        Returns:
            List of components from the specified file
        """
        component_ids = self._file_index.get(file_path, set())
        return [
            self._components[comp_id] for comp_id in component_ids if comp_id in self._components
        ]

    def clear(self) -> None:
        """Clear all components from the catalog."""
        self._components.clear()
        self._name_index.clear()
        self._type_index.clear()
        self._file_index.clear()
        # Reset the overwrite tally too, so a catalog reused across runs
        # (analyze_directory calls clear() at the start of each run) reports the
        # current run's collisions, not an accumulated total.
        self._overwritten_count = 0
        logger.info("Component catalog cleared")

    def get_component_count(self) -> int:
        """
        Get total number of components in the catalog.

        Returns:
            Total component count
        """
        return len(self._components)

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get catalog statistics.

        Returns:
            Dictionary containing catalog statistics
        """
        stats = {
            "total_components": len(self._components),
            "components_by_type": {},
            "components_by_language": {},
            "files_analyzed": len(self._file_index),
            "overwritten_components": self._overwritten_count,
        }

        # Count by type
        for component in self._components.values():
            comp_type = component.type.value
            stats["components_by_type"][comp_type] = (
                stats["components_by_type"].get(comp_type, 0) + 1
            )

        # Count by language
        for component in self._components.values():
            language = component.language
            stats["components_by_language"][language] = (
                stats["components_by_language"].get(language, 0) + 1
            )

        return stats

    def _update_indexes(self, component: Component) -> None:
        """Update search indexes for a component."""
        # Name index. Callers (e.g. the MCP layer) resolve by the fully-qualified
        # id, so always index the id — and additionally index the display name
        # when it differs, so a divergent human-readable name never breaks
        # id-based lookup.
        self._name_index.setdefault(component.id, set()).add(component.id)
        if component.name != component.id:
            self._name_index.setdefault(component.name, set()).add(component.id)

        # Type index
        if component.type not in self._type_index:
            self._type_index[component.type] = set()
        self._type_index[component.type].add(component.id)

        # File index
        if component.file_path not in self._file_index:
            self._file_index[component.file_path] = set()
        self._file_index[component.file_path].add(component.id)

    @staticmethod
    def _pattern_segments(pattern: str) -> List[str]:
        """
        Split a wildcard pattern into the literal segments between its '*' wildcards.

        Segments are lower-cased so that matching is case-insensitive.

        Empty segments — produced by leading, trailing, or consecutive wildcards —
        constrain nothing and are dropped. Dropping them is what makes a pattern
        like '**********a' cost the same as '*a'.

        Args:
            pattern: Search pattern with '*' wildcards

        Returns:
            Lower-cased literal segments, in order
        """
        return [segment for segment in pattern.lower().split("*") if segment]

    @staticmethod
    def _matches_segments(segments: List[str], name: str) -> bool:
        """
        Test whether ``name`` contains every segment, in order, case-insensitively.

        This is an unanchored match: the first segment may start anywhere and the
        last may end anywhere, so 'User' matches 'MyUserService' and 'a*b' matches
        'xaybz'.

        The scan walks left to right with ``str.find``, taking the earliest
        position for each segment. That is O(len(name) x len(pattern)) with no
        backtracking. Taking the earliest position never rules out a match that a
        later placement would have found, so this is equivalent to the ordered
        wildcard match it implements while remaining immune to the catastrophic
        backtracking a regex translation of the same pattern would suffer.

        Args:
            segments: Lower-cased segments from :meth:`_pattern_segments`
            name: Component name to test

        Returns:
            True if every segment is present in order
        """
        haystack = name.lower()
        position = 0

        for segment in segments:
            found = haystack.find(segment, position)
            if found == -1:
                return False
            position = found + len(segment)

        return True


class SimpleBusinessBehaviorAnalyzer(IBusinessBehaviorAnalyzer):
    """
    Simple business behavior analyzer using pattern matching and heuristics.

    Provides basic business behavior analysis without external AI services,
    suitable for MVP implementation.
    """

    def __init__(self):
        """Initialize the analyzer with pattern definitions."""
        self._business_patterns = {
            "authentication": ["auth", "login", "password", "token", "credential"],
            "authorization": ["permission", "role", "access", "authorize", "grant"],
            "validation": ["validate", "verify", "check", "ensure", "confirm"],
            "data_processing": ["process", "transform", "convert", "parse", "format"],
            "persistence": ["save", "store", "persist", "database", "repository"],
            "retrieval": ["get", "find", "search", "query", "fetch", "load"],
            "calculation": ["calculate", "compute", "sum", "total", "count"],
            "communication": ["send", "receive", "notify", "message", "email"],
            "configuration": ["config", "setting", "option", "parameter"],
            "logging": ["log", "trace", "debug", "monitor", "audit"],
        }

        self._design_patterns = {
            "factory": ["factory", "create", "builder"],
            "singleton": ["singleton", "instance"],
            "observer": ["observer", "listener", "notify", "subscribe"],
            "strategy": ["strategy", "algorithm", "policy"],
            "decorator": ["decorator", "wrapper", "enhance"],
            "adapter": ["adapter", "wrapper", "convert"],
            "repository": ["repository", "dao", "data_access"],
            "service": ["service", "manager", "handler"],
            "controller": ["controller", "handler", "endpoint"],
            "model": ["model", "entity", "data"],
        }

    def analyze_business_behavior(self, component: Component) -> BusinessBehavior:
        """
        Analyze business behavior of a component.

        Args:
            component: Component to analyze

        Returns:
            BusinessBehavior with analysis results
        """
        # Analyze component name and documentation
        text_to_analyze = component.name.lower()
        if component.attributes.documentation:
            text_to_analyze += " " + component.attributes.documentation.lower()

        # Identify domain concepts
        domain_concepts = self._identify_domain_concepts(text_to_analyze)

        # Detect design patterns
        design_patterns = self._detect_design_patterns(text_to_analyze, component)

        # Generate description
        description = self._generate_description(component, domain_concepts, design_patterns)

        # Calculate confidence score
        confidence = self._calculate_confidence(domain_concepts, design_patterns, component)

        return BusinessBehavior(
            description=description,
            domain_concepts=domain_concepts,
            design_patterns=design_patterns,
            confidence_score=confidence,
        )

    def identify_domain_concepts(self, components: List[Component]) -> Dict[str, List[str]]:
        """
        Identify domain concepts across components.

        Args:
            components: List of components to analyze

        Returns:
            Dictionary mapping concepts to component names
        """
        concept_map: Dict[str, List[str]] = {}

        for component in components:
            behavior = self.analyze_business_behavior(component)
            for concept in behavior.domain_concepts:
                if concept not in concept_map:
                    concept_map[concept] = []
                concept_map[concept].append(component.name)

        return concept_map

    def detect_design_patterns(self, components: List[Component]) -> Dict[str, List[str]]:
        """
        Detect design patterns in components.

        Args:
            components: List of components to analyze

        Returns:
            Dictionary mapping patterns to component names
        """
        pattern_map: Dict[str, List[str]] = {}

        for component in components:
            behavior = self.analyze_business_behavior(component)
            for pattern in behavior.design_patterns:
                if pattern not in pattern_map:
                    pattern_map[pattern] = []
                pattern_map[pattern].append(component.name)

        return pattern_map

    def _identify_domain_concepts(self, text: str) -> List[str]:
        """Identify business domain concepts in text."""
        concepts = []

        for concept, keywords in self._business_patterns.items():
            if any(keyword in text for keyword in keywords):
                concepts.append(concept)

        return concepts

    def _detect_design_patterns(self, text: str, component: Component) -> List[str]:
        """Detect design patterns in component."""
        patterns = []

        for pattern, keywords in self._design_patterns.items():
            if any(keyword in text for keyword in keywords):
                patterns.append(pattern)

        # Additional pattern detection based on component structure
        if component.type == ComponentType.CLASS:
            if "manager" in component.name.lower() or "service" in component.name.lower():
                patterns.append("service")
            if "factory" in component.name.lower():
                patterns.append("factory")

        return list(set(patterns))  # Remove duplicates

    def _generate_description(
        self, component: Component, concepts: List[str], patterns: List[str]
    ) -> str:
        """Generate natural language description."""
        if component.attributes.documentation:
            return component.attributes.documentation

        # Generate basic description based on type and concepts
        type_desc = {
            ComponentType.FUNCTION: "function that",
            ComponentType.METHOD: "method that",
            ComponentType.CLASS: "class that represents",
            ComponentType.MODULE: "module containing",
            ComponentType.VARIABLE: "variable storing",
            ComponentType.CONSTANT: "constant defining",
        }.get(component.type, "component that")

        if concepts:
            concept_desc = f"handles {', '.join(concepts[:2])}"
        else:
            concept_desc = "provides functionality"

        return f"A {type_desc} {concept_desc}"

    def _calculate_confidence(
        self, concepts: List[str], patterns: List[str], component: Component
    ) -> float:
        """Calculate confidence score for the analysis."""
        base_confidence = 0.3

        # Increase confidence based on available information
        if component.attributes.documentation:
            base_confidence += 0.4

        if concepts:
            base_confidence += 0.2 * min(len(concepts), 2)

        if patterns:
            base_confidence += 0.1 * min(len(patterns), 2)

        return min(base_confidence, 1.0)
