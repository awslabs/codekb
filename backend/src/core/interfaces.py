"""
Core interfaces for the Code Knowledge Base system.

This module defines the abstract interfaces that enable dependency inversion
and loose coupling between components following the Hexagonal Architecture pattern.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from .models import Component, ExecutionFlow, DependencyAnalysis, BusinessBehavior


class IComponentRepository(ABC):
    """
    Interface for component storage and retrieval operations.

    This interface abstracts the component catalog functionality,
    allowing different implementations for storage and search.
    """

    @abstractmethod
    def add_component(self, component: Component) -> None:
        """
        Add a component to the repository.

        Args:
            component: The component to add

        Raises:
            ValueError: If component is invalid or already exists
        """
        pass

    @abstractmethod
    def find_component(self, name: str) -> Optional[Component]:
        """
        Find a component by exact name match.

        Args:
            name: Exact component name to find

        Returns:
            Component if found, None otherwise
        """
        pass

    @abstractmethod
    def search_components(self, pattern: str) -> List[Component]:
        """
        Search components by name pattern.

        Args:
            pattern: Search pattern (supports wildcards)

        Returns:
            List of matching components, ranked by relevance
        """
        pass

    @abstractmethod
    def get_all_components(self) -> List[Component]:
        """
        Get all components in the repository.

        Returns:
            List of all stored components
        """
        pass

    @abstractmethod
    def get_components_by_type(self, component_type: str) -> List[Component]:
        """
        Get components filtered by type.

        Args:
            component_type: Type to filter by (function, class, module, etc.)

        Returns:
            List of components of the specified type
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all components from the repository."""
        pass


class IFlowAnalyzer(ABC):
    """
    Interface for flow analysis and dependency tracking.

    This interface abstracts the flow analysis functionality,
    enabling different implementations for call graph construction
    and execution flow tracing.
    """

    @abstractmethod
    def build_call_graph(self, components: List[Component]) -> Dict[str, List[str]]:
        """
        Build a call graph from components.

        Args:
            components: List of components to analyze

        Returns:
            Dictionary mapping component names to their called components
        """
        pass

    @abstractmethod
    def trace_execution_flow(self, start_component: str, depth: int = 10) -> ExecutionFlow:
        """
        Trace execution flow from a starting component.

        Args:
            start_component: Name of the component to start tracing from
            depth: Maximum depth to trace (default: 10)

        Returns:
            ExecutionFlow object containing the traced flow

        Raises:
            ValueError: If start_component is not found
        """
        pass

    @abstractmethod
    def analyze_dependencies(self, component: str) -> DependencyAnalysis:
        """
        Analyze dependencies for a specific component.

        Args:
            component: Name of the component to analyze

        Returns:
            DependencyAnalysis object with dependency information

        Raises:
            ValueError: If component is not found
        """
        pass

    @abstractmethod
    def find_dependency_path(self, from_component: str, to_component: str) -> Optional[List[str]]:
        """
        Find dependency path between two components.

        Args:
            from_component: Starting component name
            to_component: Target component name

        Returns:
            List of component names in the dependency path, or None if no path exists
        """
        pass

    @abstractmethod
    def detect_circular_dependencies(self) -> List[List[str]]:
        """
        Detect circular dependencies in the call graph.

        Returns:
            List of circular dependency chains (each chain is a list of component names)
        """
        pass


class IBusinessBehaviorAnalyzer(ABC):
    """
    Interface for AI-powered business behavior analysis.

    This interface abstracts the business behavior analysis functionality,
    allowing different implementations for interpreting component purposes.
    """

    @abstractmethod
    def analyze_business_behavior(self, component: Component) -> BusinessBehavior:
        """
        Analyze the business behavior of a component.

        Args:
            component: Component to analyze

        Returns:
            BusinessBehavior object with analysis results
        """
        pass

    @abstractmethod
    def identify_domain_concepts(self, components: List[Component]) -> Dict[str, List[str]]:
        """
        Identify business domain concepts across components.

        Args:
            components: List of components to analyze

        Returns:
            Dictionary mapping domain concepts to related component names
        """
        pass

    @abstractmethod
    def detect_design_patterns(self, components: List[Component]) -> Dict[str, List[str]]:
        """
        Detect design patterns in the component structure.

        Args:
            components: List of components to analyze

        Returns:
            Dictionary mapping pattern names to participating component names
        """
        pass


class IConfigurationManager(ABC):
    """
    Interface for configuration management.

    This interface abstracts configuration loading and access,
    supporting different configuration sources and formats.
    """

    @abstractmethod
    def load_configuration(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Load configuration from file or default location.

        Args:
            config_path: Optional path to configuration file

        Returns:
            Dictionary containing configuration values

        Raises:
            FileNotFoundError: If configuration file is not found
            ValueError: If configuration format is invalid
        """
        pass

    @abstractmethod
    def get_config_value(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value by key.

        Args:
            key: Configuration key (supports dot notation for nested values)
            default: Default value if key is not found

        Returns:
            Configuration value or default
        """
        pass

    @abstractmethod
    def validate_configuration(self) -> List[str]:
        """
        Validate the current configuration.

        Returns:
            List of validation error messages (empty if valid)
        """
        pass
