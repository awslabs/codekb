"""Business relevance scoring for analyzed code components.

This module intentionally stays LLM-free. It estimates which functions and
methods are worth expensive summarization by combining cheap component metadata
with graph-level call relationships after the call graph has been built.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Set

from ..core.models import Component, ComponentType, Visibility
from ..core.logging_utils import get_logger

logger = get_logger(__name__)


class BusinessRelevanceAnalyzer:
    """Compute final business relevance scores from component and graph signals."""

    _BUSINESS_TERMS = {
        "account",
        "approve",
        "audit",
        "auth",
        "authorize",
        "bill",
        "billing",
        "cancel",
        "cart",
        "checkout",
        "claim",
        "collect",
        "confirm",
        "create",
        "credential",
        "customer",
        "delete",
        "discount",
        "eligibility",
        "email",
        "enroll",
        "fulfill",
        "invoice",
        "login",
        "notify",
        "order",
        "payment",
        "policy",
        "price",
        "process",
        "purchase",
        "refund",
        "register",
        "report",
        "request",
        "reserve",
        "review",
        "risk",
        "settle",
        "ship",
        "submit",
        "tax",
        "transaction",
        "user",
        "validate",
        "verify",
    }

    _BUSINESS_PATH_TERMS = {
        "api",
        "application",
        "controller",
        "domain",
        "endpoint",
        "handler",
        "route",
        "service",
        "usecase",
        "workflow",
    }

    _TECHNICAL_TERMS = {
        "adapter",
        "cache",
        "config",
        "convert",
        "debug",
        "format",
        "helper",
        "init",
        "log",
        "logger",
        "main",
        "map",
        "mapper",
        "middleware",
        "parse",
        "setup",
        "test",
        "util",
        "utils",
        "wrapper",
    }

    _TECHNICAL_PATH_TERMS = {
        "__pycache__",
        "build",
        "common",
        "config",
        "dist",
        "generated",
        "infra",
        "logging",
        "migration",
        "migrations",
        "test",
        "tests",
        "util",
        "utils",
        "vendor",
    }

    def __init__(self, repo_business_context: dict[str, Any] | None = None) -> None:
        repo_business_context = repo_business_context or {}
        self._repo_business_terms = self._clean_context_terms(
            repo_business_context.get("business_terms", [])
        )
        self._repo_technical_terms = self._clean_context_terms(
            repo_business_context.get("technical_terms", [])
        )
        self._repo_business_modules = self._clean_context_terms(
            repo_business_context.get("business_modules", [])
        )
        self._repo_entrypoint_patterns = self._clean_context_terms(
            repo_business_context.get("entrypoint_patterns", [])
        )

    def score_components(
        self,
        components: Iterable[Component],
        call_graph: Dict[str, List[Any]],
    ) -> None:
        """Populate business relevance fields on each component in-place."""
        component_by_id = {component.id: component for component in components}
        callable_ids = {
            component.id
            for component in component_by_id.values()
            if component.type in (ComponentType.FUNCTION, ComponentType.METHOD)
        }
        normalized_graph = self._normalize_call_graph(call_graph, callable_ids)
        reverse_graph = self._build_reverse_graph(normalized_graph)
        depths = self._calculate_depths(normalized_graph)

        scored = 0
        for component in component_by_id.values():
            if component.type not in (ComponentType.FUNCTION, ComponentType.METHOD):
                continue

            fan_out = len(normalized_graph.get(component.id, []))
            fan_in = len(reverse_graph.get(component.id, []))
            depth = depths.get(component.id, 0)
            score, tier, reasons = self._score_component(
                component=component,
                depth=depth,
                fan_in=fan_in,
                fan_out=fan_out,
            )
            component.business_behavior.business_relevance_score = score
            component.business_behavior.business_relevance_tier = tier
            component.business_behavior.business_relevance_reasons = reasons
            scored += 1

        logger.info(f"Business relevance scored for {scored} callable component(s)")

    def _score_component(
        self,
        component: Component,
        depth: int,
        fan_in: int,
        fan_out: int,
    ) -> tuple[float, str, list[str]]:
        score = 0.0
        reasons: list[str] = []
        name_text = component.id
        path_text = self._path_text(component.file_path)
        contributions = self._score_contributions(
            component=component,
            depth=depth,
            fan_in=fan_in,
            fan_out=fan_out,
        )

        if depth:
            contribution = contributions["call_depth"]
            score += contribution
            reasons.append(f"call depth {depth} (+{contribution:.0f})")

        if fan_out:
            contribution = contributions["fan_out"]
            score += contribution
            reasons.append(f"calls {fan_out} component(s) (+{contribution:.0f})")

        if fan_in:
            contribution = contributions["fan_in"]
            score += contribution
            reasons.append(f"called by {fan_in} component(s) (+{contribution:.0f})")

        business_name_hits = self._term_hits(name_text, self._BUSINESS_TERMS)
        if business_name_hits:
            contribution = contributions["generic_business_name_hits"]
            score += contribution
            reasons.append(
                f"business terms in name: {', '.join(business_name_hits[:3])} (+{contribution:.0f})"
            )

        repo_business_name_hits = self._term_hits(name_text, self._repo_business_terms)
        if repo_business_name_hits:
            contribution = contributions["repo_business_name_hits"]
            score += contribution
            reasons.append(
                f"repo business terms in name: {', '.join(repo_business_name_hits[:3])} "
                f"(+{contribution:.0f})"
            )

        business_path_hits = self._term_hits(path_text, self._BUSINESS_PATH_TERMS)
        if business_path_hits:
            contribution = contributions["generic_business_path_hits"]
            score += contribution
            reasons.append(
                f"business path: {', '.join(business_path_hits[:3])} (+{contribution:.0f})"
            )

        repo_business_path_hits = self._term_hits(path_text, self._repo_business_modules)
        if repo_business_path_hits:
            contribution = contributions["repo_business_path_hits"]
            score += contribution
            reasons.append(
                f"repo business module: {', '.join(repo_business_path_hits[:3])} "
                f"(+{contribution:.0f})"
            )

        entrypoint_hits = self._term_hits(name_text, self._repo_entrypoint_patterns)
        if entrypoint_hits:
            contribution = contributions["repo_entrypoint_hits"]
            score += contribution
            reasons.append(
                f"repo entrypoint pattern: {', '.join(entrypoint_hits[:3])} (+{contribution:.0f})"
            )

        if component.business_behavior.domain_concepts:
            contribution = contributions["domain_concepts"]
            score += contribution
            reasons.append(f"domain concepts (+{contribution:.0f})")

        if component.business_behavior.design_patterns:
            pattern_hits = [
                pattern
                for pattern in component.business_behavior.design_patterns
                if pattern in {"controller", "service", "repository", "strategy"}
            ]
            if pattern_hits:
                contribution = contributions["business_facing_patterns"]
                score += contribution
                reasons.append(f"business-facing pattern (+{contribution:.0f})")

        if component.attributes.documentation:
            score += contributions["documentation"]
            reasons.append("documentation present (+10)")

        if component.attributes.visibility == Visibility.PUBLIC:
            score += contributions["public_visibility"]
            reasons.append("public visibility (+8)")

        if component.attributes.complexity_score > 1:
            contribution = contributions["complexity"]
            score += contribution
            reasons.append(
                f"complexity {component.attributes.complexity_score} (+{contribution:.0f})"
            )

        line_count = component.attributes.line_count or (
            component.end_line_number - component.line_number + 1
        )
        if line_count >= 20:
            score += contributions["substantial_implementation"]
            reasons.append("substantial implementation (+8)")
        elif line_count <= 3:
            score += contributions["very_small_implementation"]
            reasons.append("very small implementation (-10)")

        technical_name_hits = self._term_hits(name_text, self._TECHNICAL_TERMS)
        if technical_name_hits:
            penalty = contributions["generic_technical_name_hits"]
            score += penalty
            reasons.append(
                f"technical terms in name: {', '.join(technical_name_hits[:3])} ({penalty:.0f})"
            )

        repo_technical_name_hits = self._term_hits(name_text, self._repo_technical_terms)
        if repo_technical_name_hits:
            penalty = contributions["repo_technical_name_hits"]
            score += penalty
            reasons.append(
                f"repo technical terms in name: {', '.join(repo_technical_name_hits[:3])} "
                f"({penalty:.0f})"
            )

        technical_path_hits = self._term_hits(path_text, self._TECHNICAL_PATH_TERMS)
        if technical_path_hits:
            penalty = contributions["generic_technical_path_hits"]
            score += penalty
            reasons.append(f"technical path: {', '.join(technical_path_hits[:3])} ({penalty:.0f})")

        score = max(0.0, min(100.0, score))
        tier = self._tier_for_score(score)
        return round(score, 1), tier, reasons

    def _score_contributions(
        self,
        component: Component,
        depth: int,
        fan_in: int,
        fan_out: int,
    ) -> dict[str, float]:
        name_text = component.id
        path_text = self._path_text(component.file_path)
        line_count = component.attributes.line_count or (
            component.end_line_number - component.line_number + 1
        )
        business_patterns = [
            pattern
            for pattern in component.business_behavior.design_patterns
            if pattern in {"controller", "service", "repository", "strategy"}
        ]
        return {
            "call_depth": float(min(depth * 8, 32)) if depth else 0.0,
            "fan_in": float(min(fan_in * 4, 20)) if fan_in else 0.0,
            "fan_out": float(min(fan_out * 3, 18)) if fan_out else 0.0,
            "generic_business_name_hits": float(
                min(len(self._term_hits(name_text, self._BUSINESS_TERMS)) * 6, 18)
            ),
            "repo_business_name_hits": float(
                min(len(self._term_hits(name_text, self._repo_business_terms)) * 10, 30)
            ),
            "generic_business_path_hits": float(
                min(len(self._term_hits(path_text, self._BUSINESS_PATH_TERMS)) * 5, 15)
            ),
            "repo_business_path_hits": float(
                min(len(self._term_hits(path_text, self._repo_business_modules)) * 8, 24)
            ),
            "repo_entrypoint_hits": float(
                min(len(self._term_hits(name_text, self._repo_entrypoint_patterns)) * 30, 40)
            ),
            "domain_concepts": float(min(len(component.business_behavior.domain_concepts) * 5, 15)),
            "business_facing_patterns": float(min(len(business_patterns) * 4, 12)),
            "documentation": 10.0 if component.attributes.documentation else 0.0,
            "public_visibility": 8.0
            if component.attributes.visibility == Visibility.PUBLIC
            else 0.0,
            "complexity": (
                float(min(component.attributes.complexity_score * 2, 12))
                if component.attributes.complexity_score > 1
                else 0.0
            ),
            "substantial_implementation": 8.0 if line_count >= 20 else 0.0,
            "very_small_implementation": -10.0 if line_count <= 3 else 0.0,
            "generic_technical_name_hits": -float(
                min(len(self._term_hits(name_text, self._TECHNICAL_TERMS)) * 6, 18)
            ),
            "repo_technical_name_hits": -float(
                min(len(self._term_hits(name_text, self._repo_technical_terms)) * 8, 24)
            ),
            "generic_technical_path_hits": -float(
                min(len(self._term_hits(path_text, self._TECHNICAL_PATH_TERMS)) * 8, 24)
            ),
        }

    @staticmethod
    def _tier_for_score(score: float) -> str:
        if score >= 80:
            return "high"
        if score >= 50:
            return "medium"
        if score >= 25:
            return "heuristic"
        return "low"

    @staticmethod
    def _normalize_call_graph(
        call_graph: Dict[str, List[Any]], callable_ids: Set[str]
    ) -> Dict[str, List[str]]:
        normalized: Dict[str, List[str]] = {component_id: [] for component_id in callable_ids}
        for caller, calls in call_graph.items():
            if caller not in callable_ids:
                continue
            seen: set[str] = set()
            for call in calls or []:
                callee = call[0] if isinstance(call, (tuple, list)) and call else call
                if isinstance(callee, str) and callee in callable_ids and callee not in seen:
                    normalized[caller].append(callee)
                    seen.add(callee)
        return normalized

    @staticmethod
    def _build_reverse_graph(call_graph: Dict[str, List[str]]) -> Dict[str, List[str]]:
        reverse_graph: Dict[str, List[str]] = defaultdict(list)
        for caller, callees in call_graph.items():
            for callee in callees:
                reverse_graph[callee].append(caller)
        return reverse_graph

    def _calculate_depths(self, call_graph: Dict[str, List[str]]) -> Dict[str, int]:
        memo: dict[str, int] = {}
        visiting: set[str] = set()

        def depth_for(component_id: str) -> int:
            if component_id in memo:
                return memo[component_id]
            if component_id in visiting:
                return 0

            visiting.add(component_id)
            child_depths = [depth_for(callee) + 1 for callee in call_graph.get(component_id, [])]
            visiting.remove(component_id)
            memo[component_id] = max(child_depths, default=0)
            return memo[component_id]

        for component_id in call_graph:
            depth_for(component_id)
        return memo

    @staticmethod
    def _path_text(file_path: Path) -> str:
        return str(file_path).replace("\\", "/")

    @staticmethod
    def _term_hits(text: str, terms: Set[str]) -> List[str]:
        text_tokens = BusinessRelevanceAnalyzer._tokenize(text)
        return sorted(
            term
            for term in terms
            if BusinessRelevanceAnalyzer._term_matches_tokens(term, text_tokens)
        )

    @staticmethod
    def _term_matches_tokens(term: str, text_tokens: List[str]) -> bool:
        term_tokens = BusinessRelevanceAnalyzer._tokenize(term)
        if not term_tokens:
            return False

        text_token_set = set(text_tokens)
        if len(term_tokens) == 1:
            return term_tokens[0] in text_token_set

        # Phrase terms, including package-style patterns like csrcreator.main,
        # must appear as an ordered contiguous token sequence.
        window_size = len(term_tokens)
        return any(
            text_tokens[index : index + window_size] == term_tokens
            for index in range(0, len(text_tokens) - window_size + 1)
        ) or BusinessRelevanceAnalyzer._compact_phrase_matches(term_tokens, text_tokens)

    @staticmethod
    def _compact_phrase_matches(term_tokens: List[str], text_tokens: List[str]) -> bool:
        term_compact = "".join(term_tokens)
        max_window_size = min(len(text_tokens), len(term_tokens) + 3)
        for window_size in range(2, max_window_size + 1):
            for index in range(0, len(text_tokens) - window_size + 1):
                if "".join(text_tokens[index : index + window_size]) == term_compact:
                    return True
        return False

    @staticmethod
    def _tokenize(value: str) -> List[str]:
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
        value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
        return [token.lower() for token in re.split(r"[^A-Za-z0-9]+", value) if token]

    @staticmethod
    def _clean_context_terms(values: Any) -> Set[str]:
        if not isinstance(values, list):
            return set()
        return {
            value.strip().lower() for value in values if isinstance(value, str) and value.strip()
        }
