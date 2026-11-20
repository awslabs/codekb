"""Tests for computational business relevance scoring."""

from pathlib import Path

from src.analysis.business_relevance_analyzer import BusinessRelevanceAnalyzer
from src.core.models import (
    BusinessBehavior,
    Component,
    ComponentAttributes,
    ComponentType,
    Visibility,
)


def _function(
    component_id: str,
    path: str,
    *,
    docs: str | None = None,
    line_count: int = 20,
    visibility: Visibility = Visibility.PUBLIC,
) -> Component:
    return Component(
        id=component_id,
        type=ComponentType.FUNCTION,
        file_path=Path(path),
        line_number=1,
        end_line_number=max(line_count, 1),
        language="python",
        attributes=ComponentAttributes(
            documentation=docs,
            visibility=visibility,
            line_count=line_count,
        ),
        business_behavior=BusinessBehavior(
            description=docs or "",
            domain_concepts=["validation"] if "validate" in component_id.lower() else [],
        ),
    )


class TestBusinessRelevanceAnalyzer:
    def test_scores_business_workflow_higher_than_technical_helper(self):
        process_order = _function(
            "shop::services.order.process_order",
            "shop/services/order_service.py",
            docs="Processes customer orders and reserves inventory.",
            line_count=45,
        )
        validate_payment = _function(
            "shop::domain.payment.validate_payment",
            "shop/domain/payment.py",
            docs="Validates payment eligibility.",
            line_count=25,
        )
        format_log = _function(
            "shop::utils.format_log",
            "shop/utils/logging.py",
            line_count=2,
        )

        BusinessRelevanceAnalyzer().score_components(
            [process_order, validate_payment, format_log],
            {
                process_order.id: [(validate_payment.id, 10), (format_log.id, 11)],
                validate_payment.id: [],
                format_log.id: [],
            },
        )

        assert process_order.business_behavior.business_relevance_score > (
            format_log.business_behavior.business_relevance_score
        )
        assert process_order.business_behavior.business_relevance_tier in {
            "medium",
            "high",
        }
        assert format_log.business_behavior.business_relevance_tier == "low"
        assert process_order.business_behavior.business_relevance_reasons
        # fan-out/fan-in are graph-derived; assert they were scored via the reasons
        # (the diagnostic signals/contributions dicts are no longer populated).
        assert any(
            "calls 2 component(s)" in reason
            for reason in process_order.business_behavior.business_relevance_reasons
        )
        assert any(
            "called by 1 component(s)" in reason
            for reason in validate_payment.business_behavior.business_relevance_reasons
        )

    def test_repo_specific_terms_increase_relevance_score(self):
        component_id = "claims::settlement.reconcile_case"
        call_graph = {component_id: []}

        generic = _function(
            component_id,
            "claims/settlement/reconciliation.py",
            line_count=12,
        )
        BusinessRelevanceAnalyzer().score_components([generic], call_graph)

        repo_specific = _function(
            component_id,
            "claims/settlement/reconciliation.py",
            line_count=12,
        )
        BusinessRelevanceAnalyzer(
            {
                "business_terms": ["claim", "settlement", "reconcile"],
                "business_modules": ["settlement"],
            }
        ).score_components([repo_specific], call_graph)

        assert repo_specific.business_behavior.business_relevance_score > (
            generic.business_behavior.business_relevance_score
        )
        assert any(
            "repo business" in reason
            for reason in repo_specific.business_behavior.business_relevance_reasons
        )

    def test_repo_entrypoint_pattern_promotes_workflow_starter_to_medium(self):
        component = _function(
            "kmscsr::com.amazonaws.kmscsr.examples.CsrCreator.main",
            "src/com/amazonaws/kmscsr/examples/CsrCreator.java",
            line_count=30,
        )

        BusinessRelevanceAnalyzer(
            {
                "business_terms": ["csr", "certificate signing request"],
                "entrypoint_patterns": ["csrcreator.main"],
            }
        ).score_components([component], {component.id: []})

        assert component.business_behavior.business_relevance_score >= 50
        assert component.business_behavior.business_relevance_tier in {"medium", "high"}
        assert any(
            "repo entrypoint pattern" in reason
            for reason in component.business_behavior.business_relevance_reasons
        )

    def test_term_matching_splits_camel_case_and_separators(self):
        hits = BusinessRelevanceAnalyzer._term_hits(
            "kmscsr::CsrCreator.createAndSignCsr",
            {"csr", "sign", "csr creator", "create and sign csr"},
        )

        assert hits == ["create and sign csr", "csr", "csr creator", "sign"]

    def test_term_matching_handles_lowercase_compacted_repo_patterns(self):
        hits = BusinessRelevanceAnalyzer._term_hits(
            "kmscsr::CsrCreator.main",
            {"csrcreator.main"},
        )

        assert hits == ["csrcreator.main"]

    def test_term_matching_avoids_substring_false_positives(self):
        hits = BusinessRelevanceAnalyzer._term_hits(
            "shop::metrics.discounter.countRisky",
            {"count", "discount", "risk"},
        )

        assert hits == ["count"]

    def test_phrase_terms_must_match_contiguous_token_sequence(self):
        hits = BusinessRelevanceAnalyzer._term_hits(
            "shop::certificate.builder.signRequest",
            {"certificate request", "certificate signing request"},
        )

        assert hits == []

    def test_business_behavior_roundtrip_keeps_score_tier_reasons(self):
        behavior = BusinessBehavior(
            description="Processes orders.",
            business_relevance_score=82.5,
            business_relevance_tier="high",
            business_relevance_reasons=["business terms in name: order (+6)"],
        )

        reloaded = BusinessBehavior.from_dict(behavior.to_dict())

        assert reloaded.business_relevance_score == 82.5
        assert reloaded.business_relevance_tier == "high"
        assert reloaded.business_relevance_reasons == ["business terms in name: order (+6)"]

    def test_diagnostic_signals_and_contributions_are_not_persisted(self):
        # signals/contributions are in-memory diagnostics only: nothing reads them
        # for a decision and the UI does not render them, so to_dict must omit them
        # to keep knowledge_base.json small.
        behavior = BusinessBehavior(
            description="Processes orders.",
            business_relevance_signals={"fan_in": 2, "fan_out": 4},
            business_relevance_contributions={"fan_in": 8.0, "fan_out": 12.0},
        )

        serialized = behavior.to_dict()

        assert "business_relevance_signals" not in serialized
        assert "business_relevance_contributions" not in serialized

    def test_old_business_behavior_payload_defaults_relevance_fields(self):
        reloaded = BusinessBehavior.from_dict({"description": "Legacy payload"})

        assert reloaded.business_relevance_score == 0.0
        assert reloaded.business_relevance_tier == "low"
        assert reloaded.business_relevance_reasons == []
        assert reloaded.business_relevance_signals == {}
        assert reloaded.business_relevance_contributions == {}


class TestBusinessBehaviorIsEmpty:
    def test_default_business_behavior_is_empty(self):
        assert BusinessBehavior().is_empty() is True

    def test_summarized_description_is_not_empty(self):
        # A disabled space can still be summarized: the LLM summary lives in
        # description, so the block must be kept.
        assert BusinessBehavior(description="Does the thing.").is_empty() is False

    def test_scored_component_is_not_empty(self):
        assert BusinessBehavior(business_relevance_tier="high").is_empty() is False
        assert BusinessBehavior(business_relevance_score=12.0).is_empty() is False
        assert BusinessBehavior(business_relevance_reasons=["x"]).is_empty() is False

    def test_analyzer_output_is_not_empty(self):
        assert BusinessBehavior(domain_concepts=["auth"]).is_empty() is False
        assert BusinessBehavior(design_patterns=["service"]).is_empty() is False
        assert BusinessBehavior(confidence_score=0.5).is_empty() is False
        assert BusinessBehavior(business_rules=["must be positive"]).is_empty() is False


class TestComponentBusinessBehaviorPersistence:
    def _component(self, behavior: BusinessBehavior) -> Component:
        return Component(
            id="pkg::mod.func",
            type=ComponentType.FUNCTION,
            file_path=Path("pkg/mod.py"),
            line_number=1,
            end_line_number=10,
            language="python",
            business_behavior=behavior,
        )

    def test_empty_block_is_omitted_from_disk(self):
        # Business-behavior feature disabled: nothing is persisted for it.
        comp = self._component(BusinessBehavior())

        serialized = comp.to_dict()

        assert "business_behavior" not in serialized

    def test_omitted_block_round_trips_to_empty_default(self):
        comp = self._component(BusinessBehavior())

        reloaded = Component.from_dict(comp.to_dict())

        # Readers that touch business_behavior still work — they see defaults.
        assert reloaded.business_behavior.is_empty()
        assert reloaded.business_behavior.business_relevance_tier == "low"
        assert reloaded.business_behavior.description == ""

    def test_populated_block_is_persisted_and_round_trips(self):
        comp = self._component(
            BusinessBehavior(
                description="Processes orders.",
                business_relevance_score=70.0,
                business_relevance_tier="high",
            )
        )

        serialized = comp.to_dict()
        assert "business_behavior" in serialized

        reloaded = Component.from_dict(serialized)
        assert reloaded.business_behavior.description == "Processes orders."
        assert reloaded.business_behavior.business_relevance_tier == "high"

    def test_summary_only_block_is_persisted(self):
        # Disabled space that was summarized: no scores, but the LLM summary in
        # description must survive serialization.
        comp = self._component(BusinessBehavior(description="An LLM summary."))

        serialized = comp.to_dict()

        assert "business_behavior" in serialized
        assert serialized["business_behavior"]["description"] == "An LLM summary."
