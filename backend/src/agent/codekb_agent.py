"""Strands-based agent for CodeKB code analysis.

Wraps CodeKBService operations as Strands @tool functions and exposes
a factory to create a multi-space-scoped agent per request.

Tools delegate to CodeKBService — the single source of truth for
business logic — with thin multi-space iteration helpers.

The agent is stateless — conversation history is managed by the caller
(frontend) and injected into a fresh Agent on each request.
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import Any, Optional

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from ..service.codekb_service import QUERYABLE_STATUSES, CodeKBService
from ..core.logging_utils import get_logger, format_payload
from ..core.token_usage import from_strands_metrics, pricing_url
from ..core.llm_settings import Workload, settings_error_from_bedrock, strands_model_kwargs
from ..core.llm_settings import resolve as resolve_llm_settings
from ..core.prompt_spotlighting import (
    publicize_markers,
    wrap_untrusted_fields,
    with_spotlight_rules,
)

logger = get_logger(__name__)

# Shared singleton — same instance used by API routes and vector modules
_codekb_service = CodeKBService.instance()

# The requested scope: an explicit list of space ids, or None meaning "every
# space". None is not the same as an enumeration of all ids — semantic_search
# turns it into an unfiltered vector query, which sidesteps S3 Vectors' 10 KB
# metadata-filter cap (~230-640 space ids, depending on id length).
_current_space_ids_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "agent_current_space_ids", default=None
)
# Concrete ids the all-spaces scope resolved to, for the per-space tools that
# cannot express "all". Kept separate so resolving it never converts the scope
# itself into a list and silently re-filters semantic_search.
_resolved_space_ids_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "agent_resolved_space_ids", default=None
)


def _scope() -> list[str] | None:
    """Return the requested scope: explicit space ids, or None for all spaces."""
    return _current_space_ids_var.get()


def _sids() -> list[str]:
    """Return concrete space ids, expanding the all-spaces scope on demand.

    Only for tools that must address spaces one at a time; anything that can
    query the whole vector store at once should use :func:`_scope` instead.
    """
    sids = _current_space_ids_var.get()
    if sids:
        return sids
    resolved = _resolved_space_ids_var.get()
    if resolved is None:
        resolved = [
            s["space_id"] for s in _codekb_service.list_spaces() if s.get("status") in QUERYABLE_STATUSES
        ]
        _resolved_space_ids_var.set(resolved)
        logger.info(f"Resolved all-spaces scope to {len(resolved)} queryable space(s)")
    if not resolved:
        raise RuntimeError("No spaces loaded.")
    return resolved


# ── multi-space iteration helpers ────────────────────────────────────────


def _first_match(method: str, not_found_msg: str, *args, **kwargs) -> str:
    """Call a service method per space, return first non-None hit."""
    for sid in _sids():
        result = getattr(_codekb_service, method)(sid, *args, **kwargs)
        if result is not None:
            if isinstance(result, dict):
                result["space_id"] = sid
            return json.dumps(wrap_untrusted_fields(result), default=str)
    return not_found_msg


def _collect_all(method: str, not_found_msg: str, *args, **kwargs) -> str:
    """Call a service method per space, merge all list results."""
    all_results = []
    for sid in _sids():
        results = getattr(_codekb_service, method)(sid, *args, **kwargs)
        for r in results:
            r["space_id"] = sid
        all_results.extend(results)
    return json.dumps(wrap_untrusted_fields(all_results), default=str) if all_results else not_found_msg


# ── tools ────────────────────────────────────────────────────────────────


@tool
def find_component(name: str) -> str:
    """Find a component by exact name across all loaded spaces.

    Args:
        name: Exact component name (e.g. 'myproject::module.ClassName.method').
    """
    return _first_match("find_component", f"Component not found: {name}", name=name)


@tool
def search_components(pattern: str) -> str:
    """Search components using wildcard patterns across all loaded spaces.

    Supports '*' wildcard. Examples: 'User*', '*Service', '*calculate*'.

    Args:
        pattern: Search pattern with wildcards.
    """
    return _collect_all("search_components", f"No components matching '{pattern}'", pattern=pattern)


@tool
def list_components(component_type: Optional[str] = None) -> str:
    """List all components, optionally filtered by type, across all loaded spaces.

    Args:
        component_type: Optional filter — 'function', 'class', 'method', 'variable', or None for all.
    """
    return _collect_all("list_components", "No components found", component_type=component_type)


@tool
def get_stats() -> str:
    """Get overall statistics about the analyzed codebases.

    Returns component counts, language breakdown, file counts, etc. for each loaded space.
    """
    all_stats = {}
    for sid in _sids():
        all_stats[sid] = _codekb_service.get_stats(sid)
    return json.dumps(wrap_untrusted_fields(all_stats), default=str)


@tool
def trace_flow(component: str, depth: int = 10) -> str:
    """Trace the execution flow starting from a function, showing all calls it makes.

    Args:
        component: Name of the function/method to trace from.
        depth: Maximum call depth to trace (default 10).
    """
    return _first_match(
        "trace_flow",
        f"Component not found or no flow: {component}",
        component=component,
        depth=depth,
    )


@tool
def show_dependencies(component: str) -> str:
    """Show what a component depends on and what depends on it.

    Args:
        component: Name of the component to analyze.
    """
    return _first_match(
        "show_dependencies",
        f"Component not found: {component}",
        component=component,
    )


@tool
def semantic_search(query: str, limit: int | None = None) -> str:
    """PREFERRED TOOL — Start here for any question about what the codebase does.

    Semantic search for components by purpose, behavior, or domain concept across all
    loaded spaces. This is the fastest way to find relevant code when you have a
    natural-language question or topic (e.g., 'advanced operations', 'process payments',
    'authentication flow', 'validate user input').

    Use this BEFORE find_component, search_components, or list_components unless you
    already know the exact component name. Returns rich descriptions that often make
    follow-up tool calls unnecessary.

    Each result's ``id`` is the component's fully-qualified identifier in the form
    '<space>::<module.ClassName.method>' (e.g. 'myproject::main.main'). When you pass a
    result into trace_flow, show_dependencies, get_component_source, or find_component,
    use this ``id`` string EXACTLY as returned — including the leading '<space>::'
    prefix. Never abbreviate it or strip the prefix; doing so causes "Component not
    found" errors.

    Args:
        query: Natural language description (e.g. 'process payments', 'validate user input').
        limit: Maximum number of results. Defaults to ``storage.s3_vectors.default_top_k``
            from config when omitted.
    """
    scope = _scope()
    logger.info(
        f"[tool.semantic_search] query={query!r}, limit={limit}, "
        f"spaces={'<all>' if scope is None else len(scope)}"
    )
    results = _codekb_service.global_semantic_search(query, n_results=limit, space_ids=scope)
    logger.info(f"[tool.semantic_search] returned {len(results)} result(s) for query={query!r}")
    if not results:
        return f"No components found for: '{query}'"
    output = []
    for r in results:
        meta = r.get("metadata", {})
        output.append(
            {
                "id": r.get("key"),
                "description": meta.get("description", ""),
                "type": meta.get("type", ""),
                "file_path": meta.get("file_path", ""),
                "space_id": meta.get("space_id", ""),
            }
        )
    return json.dumps(wrap_untrusted_fields(output), default=str)


@tool
def get_component_source(name: str) -> str:
    """Get the full source code of a component by exact name.

    Returns the complete source code along with file path and line numbers.
    Essential for understanding implementation details and suggesting code changes.

    Args:
        name: Exact component name (e.g. 'myproject::module.ClassName.method').
    """
    for sid in _sids():
        result = _codekb_service.find_component(sid, name=name)
        if result is not None:
            source = result.get("source_code", "")
            if not source:
                return f"Component '{name}' found but has no source code stored."
            return json.dumps(
                wrap_untrusted_fields(
                    {
                        "name": result.get("name"),
                        "type": result.get("type"),
                        "file_path": result.get("file_path"),
                        "line_number": result.get("line_number"),
                        "end_line_number": result.get("end_line_number"),
                        "source_code": source,
                        "space_id": sid,
                    }
                ),
                default=str,
            )
    return f"Component not found: {name}"


@tool
def read_source_file(file_path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read the contents of a source file from the analyzed codebase.

    Use this to read files that aren't individual components — config files, module-level
    code, imports, test files, etc. Also useful to see the full file context around a
    component (e.g. class-level imports, constants, or sibling methods).

    The file_path should be relative to the project root, matching what find_component
    and search_components return in their file_path field.

    Args:
        file_path: Relative path to the file (e.g. 'src/main.py', 'config/settings.yaml').
        start_line: Optional start line (1-based). 0 means from the beginning.
        end_line: Optional end line (1-based, inclusive). 0 means to the end.
    """
    for sid in _sids():
        try:
            content = _codekb_service.read_source(sid, file_path)
        except ValueError:
            return f"Access denied: path traversal detected in '{file_path}'"
        except FileNotFoundError:
            continue
        except Exception:
            continue

        try:
            lines = content.splitlines()
            total = len(lines)

            # Apply line range if specified
            s = max(0, start_line - 1) if start_line > 0 else 0
            e = min(total, end_line) if end_line > 0 else total
            selected = lines[s:e]

            return json.dumps(
                wrap_untrusted_fields(
                    {
                        "file_path": file_path,
                        "space_id": sid,
                        "total_lines": total,
                        "showing_lines": f"{s + 1}-{s + len(selected)}",
                        "content": "\n".join(selected),
                    }
                ),
                default=str,
            )
        except Exception as ex:
            return f"Error reading '{file_path}': {ex}"

    return f"File not found: '{file_path}' (searched {len(_sids())} space(s))"


@tool
def list_source_files(directory: str = "") -> str:
    """List files in the source directory of the analyzed codebase.

    Use this to discover what files exist in the project. Useful when you need to
    find config files, test files, or understand the project structure.

    Args:
        directory: Subdirectory to list (relative to project root). Empty string for root.
    """
    for sid in _sids():
        try:
            children = _codekb_service.list_source(sid, directory)
        except ValueError:
            return f"Access denied: path traversal detected in '{directory}'"
        except (FileNotFoundError, NotADirectoryError):
            continue
        except Exception:
            continue

        entries = [
            {"path": rel_path, "type": "dir" if is_dir else "file"} for rel_path, is_dir in children
        ]
        return json.dumps(
            wrap_untrusted_fields(
                {"space_id": sid, "directory": directory or ".", "entries": entries}
            ),
            default=str,
        )

    return f"Directory not found: '{directory}' (searched {len(_sids())} space(s))"


# ── system prompts ───────────────────────────────────────────────────────

CHAT_PROMPT = """You are CodeKB Agent (Hopper), an AI-powered code analysis assistant.

You help developers understand codebases by using the analysis tools available to you.
You have access to tools that let you:
- Find and search for components (functions, classes, methods)
- Trace execution flows and call graphs
- Analyze dependencies between components
- Search by natural language descriptions (semantic search)
- Read full source code of any component
- Read arbitrary source files (config, tests, module-level code)
- List files in the project directory
- Get codebase statistics

Guidelines:
- The spaces are already loaded for you. Start using analysis tools directly.
- ALWAYS start with semantic_search for any question about what code does, what features
  exist, or how something works. It finds the most relevant components by meaning, not
  just by name, and returns rich context that often answers the question in one call.
- Study the results from semantic_search and choose the best candidates that match the user's task
- Only fall back to find_component or search_components when you already know the exact
  component name or need a wildcard pattern match.
- For "how does X work" questions, start with semantic_search, then use trace_flow on
  the top results to show the execution path.
- For "what calls X" or "what does X depend on", use show_dependencies.
- You cannot fetch external URLs. If the user references a GitHub issue, PR, or doc page,
  ask them to paste the relevant content, then cross-reference it with the codebase using
  the analysis tools. Never guess at the contents of a link you were given.
- Use get_component_source when you need to see the actual implementation of a function.
- Use read_source_file to read full files including config, tests, or module-level code.
- Use list_source_files to discover project structure and find files.
- Be concise and focus on the code analysis results.
- Format your responses clearly with component names, file paths, and relevant details.
"""

IMPACT_ANALYSIS_PROMPT = """You are an expert senior software developer operating in Impact Analysis mode.

Your job is to analyze defects or enhancements against the loaded codebase
and produce a precise, actionable implementation plan with code-level detail.

You MUST follow this structured workflow for every request:

## Phase 1 — Understand the Problem
- You cannot fetch external URLs. If the user provides one, ask them to paste the issue or
  PR content directly. Never guess at the contents of a link you were given.
- Extract: problem statement, expected vs actual behavior, error messages, and any hints
  about which parts of the system are involved.
- If the user describes the problem directly, summarize your understanding before proceeding.

## Phase 2 — Locate the Code (Precision)
- Use semantic_search with multiple queries derived from the problem to find relevant components.
- Study the results from semantic_search and choose the best candidates that match the user's task
- For each candidate, use get_component_source to read the actual implementation.
- Use read_source_file to see the full file context (imports, constants, class definitions).
- Use list_source_files to explore the project structure if needed.
- Narrow down to the EXACT functions/methods that need to change.
- Explain WHY each identified component is relevant to the problem.

## Phase 3 — Trace the Impact (Recall)
- For EVERY component you identified in Phase 2, use show_dependencies to find:
  - All callers (dependents) — these may need updates or could break.
  - All callees (dependencies) — these provide context for the fix.
- Use trace_flow on key components to understand the full execution path.
- Build a complete map of the blast radius: every component that could be affected.
- In your response, LIST each component you traced with its callers and callees.
  Show the actual dependency data you retrieved — do NOT summarize as "I now understand".
  The user must see the blast radius map to trust the analysis.

## Phase 4 — Produce the Plan
Structure your final response as:

### Root Cause Analysis
Brief explanation of why the defect exists or what needs to change for the enhancement.

### Changes Required
For each file/component that needs modification:
- File path and component name
- Current code (relevant snippet from get_component_source)
- Proposed change with explanation
- Use code blocks with the language for syntax highlighting

### Impact Assessment
For each component in the blast radius:
- Component name and file path
- How it's connected (caller/callee/shared state)
- Whether it needs changes, testing, or just awareness
- Risk level (high/medium/low)

### Testing Recommendations
- Which components need unit test updates
- Integration test scenarios to validate the change
- Edge cases to watch for

## Rules
- NEVER skip Phase 3. Recall is critical — missing an impacted component causes regressions.
- ALWAYS show actual source code from get_component_source, not paraphrased descriptions.
- ALWAYS include ALL four phases in your response, clearly labeled with their phase headers.
  Users need to see your full reasoning process — from understanding the problem, through
  locating code and tracing impact, to the final plan. Do NOT jump straight to the plan.
- EVERY phase must contain substantive output showing what you found or analyzed:
  - Phase 1: The extracted problem statement, expected vs actual behavior, and relevant hints.
  - Phase 2: The actual components found, with file paths, line numbers, and source code snippets.
  - Phase 3: The actual dependency data — list each component with its callers and callees.
  - Phase 4: The structured plan with code changes, impact assessment, and testing recommendations.
  Do NOT write "I now understand" or "Perfect!" as a substitute for showing real data.
  The user must see the evidence behind your analysis in every phase.
- Even if the analysis is straightforward, show what you found in each phase. A short phase
  is fine; a missing or empty phase is not.
- If the codebase doesn't have enough context to fully answer, say so explicitly.
- Be thorough but structured. Developers should be able to follow your plan step by step.
"""

# Map mode names to prompts
_MODE_PROMPTS = {
    "chat": CHAT_PROMPT,
    "impact_analysis": IMPACT_ANALYSIS_PROMPT,
}


# ── agent factory ────────────────────────────────────────────────────────

_ALL_TOOLS = [
    find_component,
    search_components,
    list_components,
    get_stats,
    trace_flow,
    show_dependencies,
    semantic_search,
    get_component_source,
    read_source_file,
    list_source_files,
]


def create_agent(
    space_ids: list[str] | None,
    mode: str = "chat",
    history: list[dict[str, Any]] | None = None,
) -> Agent:
    """Create a Strands agent scoped to one or more spaces.

    The agent is stateless — conversation history is injected via the
    ``history`` parameter and a fresh Agent is created per request.

    History is a list of simple {role, content} dicts from the frontend,
    converted to Bedrock message format here.

    Args:
        space_ids: Spaces to scope the agent to, or None for every space.
        mode: Agent mode — 'chat' or 'impact_analysis'.
        history: Prior conversation as [{role: "user"|"assistant", content: "..."}].
    """
    _current_space_ids_var.set(space_ids or None)
    _resolved_space_ids_var.set(None)

    from ..core.bedrock_guardrail import GuardrailTarget, get_guardrail_kwargs

    llm = resolve_llm_settings(Workload.CHAT)
    if not llm.model_id:
        raise ValueError("No model configured for chat (CODEKB_BEDROCK_MODEL_ID)")

    # Convert simple history to Bedrock message format
    messages = []
    if history:
        for msg in history:
            messages.append(
                {
                    "role": msg["role"],
                    "content": [{"text": msg["content"]}],
                }
            )

    model_kwargs: dict[str, Any] = strands_model_kwargs(llm)
    logger.info(
        f"Creating agent for spaces {space_ids or '<all>'}, mode={mode}, "
        f"history_len={len(messages)}, "
        f"model={llm.model_id}, region={model_kwargs['region_name']}, "
        f"max_tokens={model_kwargs['max_tokens']}, thinking={llm.thinking}, "
        f"effort={llm.effort}"
    )

    # Always-on secret/PII guardrail (see core/bedrock_guardrail). Empty unless a
    # guardrail id is configured, so local runs are unaffected.
    model_kwargs.update(get_guardrail_kwargs(GuardrailTarget.STRANDS_BEDROCK_MODEL))

    model = BedrockModel(**model_kwargs)

    prompt = _MODE_PROMPTS.get(mode, CHAT_PROMPT)
    # Tell the agent which spaces it's working on: list names when few, else just the count.
    if not space_ids:
        spaces_label = "all spaces"
    elif len(space_ids) <= 20:
        spaces_label = ", ".join(space_ids)
    else:
        spaces_label = f"{len(space_ids)} spaces"
    agent = Agent(
        model=model,
        tools=_ALL_TOOLS,
        system_prompt=with_spotlight_rules(prompt) + f"\n\nLoaded spaces: {spaces_label}",
        messages=messages,
    )

    return agent


# ── public query functions ───────────────────────────────────────────────


def run_agent_query(
    space_ids: list[str] | None,
    message: str,
    mode: str = "chat",
    history: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Run a single agent query (stateless).

    Args:
        space_ids: Space identifiers, or None for every space.
        message: User's natural language question.
        mode: Agent mode — 'chat' or 'impact_analysis'.
        history: Prior conversation messages to resume from.

    Returns:
        Tuple of (response_text, updated_history, token_usage). ``token_usage``
        is the serialized per-model Bedrock token consumption for this turn.
    """
    logger.info(f"Agent query for spaces {space_ids or '<all>'} (mode={mode}): {message[:100]}")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Agent query full input:\n%s",
            format_payload(
                {
                    "space_ids": space_ids,
                    "mode": mode,
                    "message": message,
                    "history": history or [],
                }
            ),
        )
    try:
        agent = create_agent(space_ids, mode=mode, history=history)
        result = agent(message)
        response_text = str(result)
        token_usage = from_strands_metrics(
            result, resolve_llm_settings(Workload.CHAT).model_id
        ).to_dict()
        # Build simple history for the frontend
        updated_history = list(history or [])
        updated_history.append({"role": "user", "content": message})
        updated_history.append({"role": "assistant", "content": response_text})
        logger.info(
            f"Agent query completed ({len(updated_history)} messages in history), "
            f"token usage: {token_usage.get('totals')}"
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Agent query full response:\n%s", format_payload(response_text))
        return response_text, updated_history, token_usage
    except Exception as e:
        _log_settings_error(e)
        logger.error(f"Agent query failed for spaces {space_ids or '<all>'}: {e}", exc_info=True)
        raise


async def stream_agent_query(
    space_ids: list[str] | None,
    message: str,
    mode: str = "chat",
    history: list[dict[str, Any]] | None = None,
):
    """Stream agent events (stateless).

    Yields SSE-formatted events for real-time UI updates.

    Event types:
      - tool_start: agent is calling a tool (with name and parsed input)
      - tool_done: tool finished
      - tool_result: tool output (with toolUseId and result text)
      - reasoning: incremental reasoning/thinking text from the agent
      - text: incremental text from the agent
      - done: final complete response + updated conversation history

    Args:
        space_ids: Space identifiers, or None for every space.
        message: User's natural language question.
        mode: Agent mode — 'chat' or 'impact_analysis'.
        history: Prior conversation messages to resume from.

    Yields:
        SSE-formatted strings.
    """
    agent = create_agent(space_ids, mode=mode, history=history)

    full_text = ""
    seen_tool_ids = set()
    input_complete_ids = set()
    done_tool_ids = set()
    pending_tools: dict[str, dict] = {}
    active_tool_id: str | None = None

    def _mark_tool_done(tid: str) -> str | None:
        """Return SSE string to mark a tool as done, or None if already done."""
        if tid and tid not in done_tool_ids:
            done_tool_ids.add(tid)
            return f"event: tool_done\ndata: {json.dumps({'toolUseId': tid})}\n\n"
        return None

    def _extract_tool_result_text(result: dict) -> str:
        """Flatten a Strands ToolResult's content blocks into display text."""
        parts: list[str] = []
        for block in result.get("content", []) or []:
            if "text" in block:
                parts.append(block["text"])
            elif "json" in block:
                parts.append(json.dumps(block["json"], default=str))
        # Display-only: swap the internal markers the model saw for the public
        # pair, so the internal boundary never leaves the backend.
        return publicize_markers("\n".join(parts))

    try:
        async for event in agent.stream_async(message):
            event_type = event.get("type")

            # ── Tool use streaming: accumulate tool info ──
            if event_type == "tool_use_stream":
                current_tool = event.get("current_tool_use", {})
                tool_use_id = current_tool.get("toolUseId")
                tool_name = current_tool.get("name")
                if tool_use_id and tool_name:
                    if active_tool_id and active_tool_id != tool_use_id:
                        sse = _mark_tool_done(active_tool_id)
                        if sse:
                            yield sse
                    active_tool_id = tool_use_id

                    input_str = current_tool.get("input", "")
                    pending_tools[tool_use_id] = {
                        "name": tool_name,
                        "input_str": input_str,
                    }

                    if tool_use_id not in seen_tool_ids:
                        seen_tool_ids.add(tool_use_id)
                        data = json.dumps(
                            {
                                "tool": tool_name,
                                "input": {},
                                "toolUseId": tool_use_id,
                            },
                            default=str,
                        )
                        yield f"event: tool_start\ndata: {data}\n\n"

                    if tool_use_id not in input_complete_ids and input_str:
                        try:
                            parsed = json.loads(input_str)
                            if isinstance(parsed, dict) and parsed:
                                input_complete_ids.add(tool_use_id)
                                data = json.dumps(
                                    {
                                        "tool": tool_name,
                                        "input": parsed,
                                        "toolUseId": tool_use_id,
                                    },
                                    default=str,
                                )
                                yield f"event: tool_start\ndata: {data}\n\n"
                        except (json.JSONDecodeError, TypeError):
                            pass

            # ── Reasoning / thinking text ──
            elif event.get("reasoning") and event.get("reasoningText"):
                data = json.dumps({"text": event["reasoningText"]})
                yield f"event: reasoning\ndata: {data}\n\n"

            # ── Tool results (delivered as a user message of toolResult blocks) ──
            elif (
                "message" in event
                and isinstance(event["message"], dict)
                and event["message"].get("role") == "user"
            ):
                for block in event["message"].get("content", []) or []:
                    tr = block.get("toolResult") if isinstance(block, dict) else None
                    if not tr:
                        continue
                    tid = tr.get("toolUseId")
                    result_text = _extract_tool_result_text(tr)
                    sse = _mark_tool_done(tid) if tid else None
                    if sse:
                        yield sse
                    data = json.dumps(
                        {
                            "toolUseId": tid,
                            "result": result_text,
                            "status": tr.get("status", "success"),
                        },
                        default=str,
                    )
                    yield f"event: tool_result\ndata: {data}\n\n"

            # ── Incremental text ──
            elif (
                "data" in event
                and isinstance(event["data"], str)
                and event_type != "tool_use_stream"
            ):
                if active_tool_id:
                    sse = _mark_tool_done(active_tool_id)
                    if sse:
                        yield sse
                    active_tool_id = None
                full_text += event["data"]
                data = json.dumps({"text": event["data"]})
                yield f"event: text\ndata: {data}\n\n"

            # ── Final result ──
            elif "result" in event and event_type is None:
                for tid in list(seen_tool_ids - done_tool_ids):
                    sse = _mark_tool_done(tid)
                    if sse:
                        yield sse
                final = str(event["result"]) if event["result"] else full_text
                token_usage = from_strands_metrics(
                    event["result"], resolve_llm_settings(Workload.CHAT).model_id
                ).to_dict()
                done_data = json.dumps(
                    {
                        "response": final,
                        "token_usage": token_usage,
                        "pricing_url": pricing_url(),
                    },
                    default=str,
                )
                yield f"event: done\ndata: {done_data}\n\n"
    except Exception as e:
        if _log_settings_error(e):
            # The details (model, env var) are for operators, so they stay in
            # the log; the user gets what happened and who can fix it.
            message = (
                "Bedrock rejected the chat model settings. An administrator must "
                "correct the CodeKB chat settings named in the service logs."
            )
        else:
            message = str(e)
        logger.error(f"Agent stream failed: {e}", exc_info=True)
        error_data = json.dumps({"error": message})
        yield f"event: error\ndata: {error_data}\n\n"


def _log_settings_error(exc: BaseException) -> bool:
    """Log a clear error if ``exc`` is Bedrock rejecting the chat settings.

    Returns True when it was. Resolving again is cheap and matches the settings
    the agent was built with, since configuration doesn't change at runtime.
    """
    try:
        settings_error = settings_error_from_bedrock(exc, resolve_llm_settings(Workload.CHAT))
    except Exception:  # pragma: no cover - never mask the original failure
        return False
    if settings_error is None:
        return False
    logger.error(str(settings_error))
    return True
