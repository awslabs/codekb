"""Repository-level business vocabulary extraction.

This is a deliberately small LLM stage: it asks for domain vocabulary only,
using compact repository metadata rather than full source code. The output is
used as an input to deterministic relevance scoring, not as a user-facing
summary.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..core.config import require_config
from ..core.llm_settings import (
    Workload,
    anthropic_request_fields,
    max_tokens_error,
    settings_error_from_bedrock,
)
from ..core.llm_settings import resolve as resolve_llm_settings
from ..core.models import Component, ComponentType
from ..core.token_usage import (
    TokenUsage,
    from_anthropic_invoke,
    from_openai_invoke,
    workload_record,
)
from ..core.logging_utils import ConfigurationError, format_payload, get_logger
from ..core.prompt_spotlighting import build_prompt
from ..core.bedrock_guardrail import GuardrailTarget, get_guardrail_kwargs

logger = get_logger(__name__)


class RepoBusinessContextExtractor:
    """Extract repo-specific business terms with one compact LLM call."""

    _DOC_NAMES = {
        "readme.md",
        "readme.txt",
        "overview.md",
        "architecture.md",
        "business.md",
    }

    def __init__(self) -> None:
        self._bedrock_client = None
        self._llm = resolve_llm_settings(Workload.BUSINESS_CONTEXT)
        # Tokens this extractor's calls consumed; part of the space_creation record.
        self.token_usage = TokenUsage()
        self._bedrock_region = require_config("llm.providers.bedrock.region")
        self._bedrock_read_timeout = int(require_config("llm.providers.bedrock.read_timeout"))
        self._max_input_chars = int(
            require_config("summarization.business_context.max_input_chars")
        )
        self._max_components = int(
            require_config("summarization.business_context.max_components")
        )
        self._max_classes = int(require_config("summarization.business_context.max_classes"))
        self._max_named_values = int(
            require_config("summarization.business_context.max_named_values")
        )
        self._max_doc_chars = int(require_config("summarization.business_context.max_doc_chars"))

    def extract(
        self,
        source_directory: Path,
        components: Iterable[Component],
        call_graph: Dict[str, List[Any]],
    ) -> dict[str, Any]:
        """Return repo-specific business context, or an empty context on failure."""
        if not self._llm.model_id:
            logger.warning("Skipping business context extraction: bedrock model is not configured")
            return self.empty_context("bedrock model is not configured")

        input_text = self._build_input(source_directory, components, call_graph)
        if not input_text.strip():
            return self.empty_context("no compact repository metadata available")

        prompt = """Extract repository-specific business vocabulary from the compact codebase metadata.

Return ONLY valid JSON with this exact shape:
{
  "business_terms": ["term"],
  "technical_terms": ["term"],
  "business_modules": ["module_or_path_fragment"],
  "entrypoint_patterns": ["pattern"],
  "confidence": 0.0
}

Rules:
- business_terms must be domain/business words specific to this repository.
- technical_terms must be framework, plumbing, utility, logging, config, build, or infrastructure words.
- business_modules should be path or package fragments that appear business-facing.
- entrypoint_patterns should capture names that look like APIs, handlers, controllers, workflows, or use cases.
- Class names and top-level constants/variables are supporting vocabulary; use them to infer domain terms, but do not blindly copy every name.
- Use lowercase singular terms where practical.
- Do not include generic programming words unless they are domain-specific in this repo.
- Keep each list to at most 50 items.
- Return JSON only. No markdown."""

        try:
            response = self._call_bedrock(prompt, input_text)
            context = self._parse_response(response)
            context["source"] = "llm"
            logger.info(
                "Business context extracted: "
                f"{len(context.get('business_terms', []))} business term(s), "
                f"{len(context.get('technical_terms', []))} technical term(s)"
            )
            return context
        except ConfigurationError as e:
            # Bedrock rejected the configured settings. Relevance scoring still
            # works without the vocabulary, so degrade, but say what to fix.
            logger.error(f"Business context extraction skipped: {e}")
            return self.empty_context(str(e))
        except Exception as e:
            logger.warning(f"Business context extraction failed: {e}", exc_info=True)
            return self.empty_context(str(e))

    def usage_part(self) -> dict[str, Any]:
        """This run's tokens and settings, as a part for ``operation_usage``."""
        return {
            "token_usage": self.token_usage.to_dict(),
            "llm_workload": workload_record(self._llm, self.token_usage, self._bedrock_region),
        }

    @staticmethod
    def empty_context(reason: str | None = None) -> dict[str, Any]:
        context: dict[str, Any] = {
            "business_terms": [],
            "technical_terms": [],
            "business_modules": [],
            "entrypoint_patterns": [],
            "confidence": 0.0,
            "source": "fallback",
        }
        if reason:
            context["error"] = reason
        return context

    def _build_input(
        self,
        source_directory: Path,
        components: Iterable[Component],
        call_graph: Dict[str, List[Any]],
    ) -> str:
        docs = self._read_repo_docs(source_directory)
        component_list = list(components)
        component_lines = self._component_inventory(component_list, call_graph)
        class_lines = self._class_inventory(component_list)
        named_value_lines = self._named_value_inventory(component_list)
        payload = "\n\n".join(
            section
            for section in [
                "# Repository Docs\n" + docs if docs else "",
                "# Component Inventory\n" + "\n".join(component_lines),
                "# Class Inventory\n" + "\n".join(class_lines) if class_lines else "",
                "# Named Values Inventory\n" + "\n".join(named_value_lines)
                if named_value_lines
                else "",
            ]
            if section
        )
        return payload[: self._max_input_chars]

    def _read_repo_docs(self, source_directory: Path) -> str:
        snippets: list[str] = []
        try:
            for path in sorted(source_directory.rglob("*")):
                if not path.is_file() or path.name.lower() not in self._DOC_NAMES:
                    continue
                try:
                    rel = path.relative_to(source_directory)
                    text = path.read_text(errors="replace")[: self._max_doc_chars]
                    snippets.append(f"## {rel}\n{text}")
                except Exception as e:
                    logger.debug(f"Could not read business context doc {path}: {e}")
                if sum(len(snippet) for snippet in snippets) >= self._max_doc_chars:
                    break
        except Exception as e:
            logger.debug(f"Could not scan repository docs for business context: {e}")
        return "\n\n".join(snippets)[: self._max_doc_chars]

    def _component_inventory(
        self, components: Iterable[Component], call_graph: Dict[str, List[Any]]
    ) -> list[str]:
        reverse_counts: dict[str, int] = {}
        for calls in call_graph.values():
            for call in calls or []:
                callee = call[0] if isinstance(call, (tuple, list)) and call else call
                if isinstance(callee, str):
                    reverse_counts[callee] = reverse_counts.get(callee, 0) + 1

        callable_components = [
            component
            for component in components
            if component.type in (ComponentType.FUNCTION, ComponentType.METHOD)
        ]
        callable_components.sort(
            key=lambda component: (
                -len(call_graph.get(component.id, [])),
                -reverse_counts.get(component.id, 0),
                str(component.file_path),
                component.id,
            )
        )

        lines: list[str] = []
        for component in callable_components[: self._max_components]:
            doc = component.attributes.documentation or ""
            concepts = ",".join(component.business_behavior.domain_concepts)
            patterns = ",".join(component.business_behavior.design_patterns)
            lines.append(
                " | ".join(
                    [
                        f"id={component.id}",
                        f"type={component.type.value}",
                        f"file={component.file_path}",
                        f"fan_out={len(call_graph.get(component.id, []))}",
                        f"fan_in={reverse_counts.get(component.id, 0)}",
                        f"concepts={concepts}",
                        f"patterns={patterns}",
                        f"doc={doc[:240]}",
                    ]
                )
            )
        return lines

    def _class_inventory(self, components: Iterable[Component]) -> list[str]:
        class_components = [
            component for component in components if component.type == ComponentType.CLASS
        ]
        class_components.sort(
            key=lambda component: (
                str(component.file_path),
                component.id,
            )
        )

        lines: list[str] = []
        for component in class_components[: self._max_classes]:
            doc = component.attributes.documentation or ""
            lines.append(
                " | ".join(
                    [
                        f"id={component.id}",
                        f"type={component.type.value}",
                        f"file={component.file_path}",
                        f"visibility={component.attributes.visibility.value}",
                        f"doc={doc[:240]}",
                    ]
                )
            )
        return lines

    def _named_value_inventory(self, components: Iterable[Component]) -> list[str]:
        named_values = [
            component
            for component in components
            if component.type in (ComponentType.CONSTANT, ComponentType.VARIABLE)
            and not component.parent_component
            and self._is_useful_named_value(component)
        ]
        named_values.sort(
            key=lambda component: (
                component.type != ComponentType.CONSTANT,
                str(component.file_path),
                component.id,
            )
        )

        lines: list[str] = []
        for component in named_values[: self._max_named_values]:
            doc = component.attributes.documentation or ""
            lines.append(
                " | ".join(
                    [
                        f"id={component.id}",
                        f"type={component.type.value}",
                        f"file={component.file_path}",
                        f"visibility={component.attributes.visibility.value}",
                        f"doc={doc[:160]}",
                    ]
                )
            )
        return lines

    @staticmethod
    def _is_useful_named_value(component: Component) -> bool:
        simple_name = component.get_simple_name().strip("_").lower()
        if len(simple_name) < 3:
            return False
        return simple_name not in {
            "arg",
            "args",
            "cfg",
            "ctx",
            "err",
            "i",
            "id",
            "j",
            "k",
            "log",
            "obj",
            "res",
            "ret",
            "tmp",
            "val",
        }

    def _get_bedrock_client(self):
        if self._bedrock_client is None:
            import boto3
            from botocore.config import Config

            config = Config(read_timeout=self._bedrock_read_timeout)
            self._bedrock_client = boto3.client(
                "bedrock-runtime", region_name=self._bedrock_region, config=config
            )
        return self._bedrock_client

    def _call_bedrock(self, prompt: str, input_text: str) -> str:
        model_id = self._llm.model_id
        request_fields = anthropic_request_fields(self._llm)
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                **request_fields,
                "messages": [{"role": "user", "content": build_prompt(prompt, input_text)}],
            }
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "business_context bedrock request:\n%s",
                format_payload(
                    {
                        "modelId": model_id,
                        "region": self._bedrock_region,
                        "input_chars": len(input_text),
                        **request_fields,
                    }
                ),
            )

        start = time.perf_counter()
        try:
            response = self._get_bedrock_client().invoke_model(
                modelId=model_id,
                body=body,
                **get_guardrail_kwargs(GuardrailTarget.BOTO3_INVOKE_MODEL),
            )
        except Exception as e:
            settings_error = settings_error_from_bedrock(e, self._llm)
            if settings_error is not None:
                raise settings_error from e
            raise
        response_body = json.loads(response["body"].read())
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(f"business_context bedrock call completed in {elapsed_ms:.0f}ms")
        try:  # accounting must never break extraction
            extract_usage = from_openai_invoke if "qwen" in model_id.lower() else from_anthropic_invoke
            self.token_usage.merge(extract_usage(response_body, model_id))
        except Exception:  # pragma: no cover - defensive
            logger.debug("business_context token usage capture failed", exc_info=True)

        if "qwen" in model_id.lower():
            return response_body["choices"][0]["message"]["content"]
        text = None
        for block in response_body["content"]:
            if block.get("type") == "text":
                text = block["text"]
                break
        if text is None:
            if response_body.get("stop_reason") == "max_tokens":
                raise max_tokens_error(self._llm)
            raise ValueError("No text content block in Bedrock response")
        return text

    @staticmethod
    def _parse_response(response: str) -> dict[str, Any]:
        json_str = response.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```", 1)[1].split("```", 1)[0].strip()

        start = json_str.find("{")
        end = json_str.rfind("}")
        if start != -1 and end != -1:
            json_str = json_str[start : end + 1]

        parsed = json.loads(json_str)
        return {
            "business_terms": RepoBusinessContextExtractor._clean_terms(
                parsed.get("business_terms", [])
            ),
            "technical_terms": RepoBusinessContextExtractor._clean_terms(
                parsed.get("technical_terms", [])
            ),
            "business_modules": RepoBusinessContextExtractor._clean_terms(
                parsed.get("business_modules", [])
            ),
            "entrypoint_patterns": RepoBusinessContextExtractor._clean_terms(
                parsed.get("entrypoint_patterns", [])
            ),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
        }

    @staticmethod
    def _clean_terms(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []

        terms: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            term = value.strip().lower()
            if not term or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= 50:
                break
        return terms
