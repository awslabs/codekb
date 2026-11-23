"""Token-aware input packing for summarization.

This is the single home for *what goes into a description prompt and how big it
gets*. It is used by both fresh indexing and delta refresh, because both run
through ``TopDownSummarizer.summarize_all`` and that is where the packer is
driven from.

The problem it solves: a call-graph-with-source prompt can far exceed the model
context window, and the previous behavior cut the text at a character boundary —
dropping the tail (the deepest callees, or the last functions of a batch) and
leaving them undescribed or poorly described. This packer instead fits each call
in COMPLETE units:

  * individual (entry-point) calls: render the entry point and as many levels of
    its call tree as fit — "max depth that fits". Callees past the fitting depth
    are shown as a one-line SIGNATURE (so the caller still knows what it calls)
    and are described on their own depth pass, not here.
  * batch calls: greedily pack as many members as fit into one call; a callee
    shared by several members is emitted once and referenced by id in each
    caller's ``calls`` field; members that do not fit roll into the next
    call. No member is ever dropped.

Each call is rendered as a JSON array of call-graph objects (one per entry
point packed into the call). Every object is ``{"root": <id>, "functions":
[<node>, ...]}`` and every node is ``{"id", "type", "status", "calls": [...],
and either "source" or "cached_description"}``. Repository source rides inside
the ``source`` JSON string (escaped), which is also the injection boundary —
a forged control token in the code cannot break out of the string.

Two invariants hold for every call it emits:

  * Complete units only — a function is either rendered with its full SOURCE or
    not asked for. The one exception is the floor: a single function whose own
    source exceeds the budget is truncated with a marker (``floored=True``).
  * ``source_ids`` lists exactly the functions rendered as full SOURCE — the
    only ids that call may request. The caller reconciles against this set, so
    truncation can never make the model appear to "miss" something it was never
    shown.

The budget is a single word count (``max_input_words``) — a model-agnostic proxy
for tokens that the summarizer derives from the operator's configured token
budget. Every fitting decision counts whitespace-delimited words, and
``_bounded_input`` enforces the same word budget as a hard backstop.
``max_input_words <= 0`` disables the cap (no fitting constraint).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, NamedTuple, Optional

# (component_id, depth_limit) -> call-graph-with-source node dict, or None.
# The node dict shape matches FlowAnalyzer.build_call_graph_with_source:
#   {"id": str, "type": str, "source": str, "calls": [<node>, ...]}
GraphFetcher = Callable[[str, int], Optional[dict]]

_SUMMARIZABLE = ("function", "method")

# ── wire-format contract ───────────────────────────────────────────────────
# The single source of truth for the JSON shape the model consumes. The packer
# emits these names/values (``_emit``, ``_floor_call``) and the summarization
# prompt describes them (TopDownSummarizer imports these), so the two cannot
# drift: rename a field here and both the emitter and the prompt follow.
FIELD_ROOT = "root"
FIELD_FUNCTIONS = "functions"
FIELD_ID = "id"
FIELD_TYPE = "type"
FIELD_STATUS = "status"
FIELD_CALLS = "calls"
FIELD_SOURCE = "source"
FIELD_CACHED = "cached_description"

STATUS_SOURCE = "SOURCE"              # new code to analyze and describe
STATUS_SIGNATURE = "SIGNATURE"        # signature-only context, not described
STATUS_CACHED = "CACHED DESCRIPTION"  # prior description, context only

# A call's data block is a JSON array of call-graph objects. These frame it and
# are the single definition of the per-call wrapper cost, so every fitting path
# sizes it the same way.
_ARRAY_OPEN = "["
_ARRAY_CLOSE = "]"
_SECTION_SEP = ", "


class _Node(NamedTuple):
    """One collected node, ready for ``_emit`` to serialize.

    ``body`` holds the function's source (SOURCE), its one-line signature
    (SIGNATURE), or its prior description (CACHED DESCRIPTION) — which one is
    implied by ``status``, so no separate discriminator is carried.
    """

    id: str
    type: Optional[str]
    status: str
    calls: List[str]
    body: str


@dataclass
class PackedCall:
    """One description call: its rendered text and the ids it may request."""

    text: str
    source_ids: List[str] = field(default_factory=list)
    # True when this is a floor call (reduced fidelity). The two flags below say
    # WHY, and are not mutually exclusive — a function with both an oversized body
    # and callees loses both. ``source_truncated``: the function's own body was
    # cut to fit. ``tree_truncated``: its call tree (callees) was dropped, so the
    # model saw no downstream context.
    floored: bool = False
    source_truncated: bool = False
    tree_truncated: bool = False
    # The call-tree depth this was rendered at (individual calls only; 0 for a
    # floor). The driver uses it to retry one level shallower when the model's
    # OUTPUT is truncated — fewer SOURCE functions shown means shorter output.
    depth: int = 0


class SummarizationPacker:
    """Build fitting description calls from call-graph-with-source trees."""

    def __init__(
        self,
        *,
        fetch_graph: GraphFetcher,
        description_cache: Dict[str, str],
        max_depth: int,
        max_input_words: int,
    ) -> None:
        self._fetch_graph = fetch_graph
        self._description_cache = description_cache
        self._max_depth = max(0, int(max_depth))
        # Word budget for one call's data block. Words are a model-agnostic proxy
        # for tokens: the summarizer derives this from a configured token budget
        # (see TopDownSummarizer). <= 0 disables the cap (unlimited).
        self._limited = int(max_input_words) > 0
        self._budget = max(1, int(max_input_words))

    @staticmethod
    def _words(text: str) -> int:
        """Whitespace-delimited word count — the budget's unit of measure."""
        return len(text.split())

    def _fits(self, text: str) -> bool:
        return (not self._limited) or self._words(text) <= self._budget

    @staticmethod
    def _wrap(sections: List[str]) -> str:
        """A call's data block: the section objects as one JSON array."""
        return f"{_ARRAY_OPEN}{_SECTION_SEP.join(sections)}{_ARRAY_CLOSE}"

    @staticmethod
    def _section_cost(section_text: str, *, first: bool) -> int:
        """Words a section adds to a call's wrapped word count.

        The first section also pays for the array brackets; each later one pays
        for the separator before it. Concatenation can only MERGE words across a
        boundary (never split them), so summing these per-section counts is an
        upper bound on ``_words(_wrap(sections))`` — i.e. it may slightly
        over-count, never under-count, so the budget is never exceeded.
        """
        if first:
            return SummarizationPacker._words(_ARRAY_OPEN + _ARRAY_CLOSE) + SummarizationPacker._words(section_text)
        return SummarizationPacker._words(_SECTION_SEP) + SummarizationPacker._words(section_text)

    # ── public API ────────────────────────────────────────────────────────

    def build_individual(self, entry_id: str, *, depth_cap: Optional[int] = None) -> List[PackedCall]:
        """One entry point → one call rendered at the deepest depth that fits.

        Tries the deepest depth down to 0 and keeps the deepest render within
        budget. Callees past the chosen depth appear as one-line signatures (not
        requested — they are described on their own depth pass). If even depth 0
        (the entry point's own source) overflows, its body is truncated with a
        marker and the call is flagged ``floored``.

        ``depth_cap`` caps the deepest depth tried (default ``max_depth``). The
        driver lowers it to retry one level shallower when the model's OUTPUT was
        truncated: fewer SOURCE functions shown ⇒ fewer descriptions ⇒ shorter
        output. The chosen depth is recorded on the returned call's ``depth``.
        """
        graph = self._fetch_graph(entry_id, self._max_depth)
        if graph is None:
            # A planned component whose call graph cannot be resolved is dropped:
            # build_individual returns [], so it is never requested and reconcile
            # (guarded on source_ids) never retries it. The fetcher is responsible
            # for logging the reason at fetch time (it alone knows the cause).
            return []

        top = self._max_depth if depth_cap is None else min(self._max_depth, max(0, depth_cap))
        for depth in range(top, -1, -1):
            call = self._render_tree(graph, depth_limit=depth)
            wrapped = self._wrap([call.text])
            if self._fits(wrapped):
                return [replace(call, text=wrapped, depth=depth)]

        # Floor: even depth 0 overflows — truncate the entry point's own body.
        # _floor_call returns an already-wrapped call.
        return [self._floor_call(graph)]

    def build_batch(self, member_ids: List[str]) -> List[PackedCall]:
        """Greedily pack members into calls; never drop a member.

        Each member's tree is rendered at full depth. A callee shared by members
        already placed in the current call is emitted once; later members simply
        reference it by id in their ``calls`` field. When the next member would
        push the call over budget, the current call is closed and a new one
        started. A single member that alone exceeds the budget falls back to
        individual depth-reduction.
        """
        calls: List[PackedCall] = []

        cur_sections: List[str] = []
        cur_source_ids: List[str] = []
        cur_seen: set[str] = set()
        cur_len = 0  # word count of _wrap(cur_sections) (conservative upper bound)

        def flush() -> None:
            nonlocal cur_sections, cur_source_ids, cur_seen, cur_len
            if cur_sections:
                calls.append(
                    PackedCall(
                        text=self._wrap(cur_sections),
                        source_ids=list(cur_source_ids),
                    )
                )
            cur_sections, cur_source_ids, cur_seen, cur_len = [], [], set(), 0

        for mid in member_ids:
            graph = self._fetch_graph(mid, self._max_depth)
            if graph is None:
                # Same drop as build_individual: a member with no resolvable call
                # graph is skipped. The fetcher logs the reason at fetch time.
                continue
            # Render against a COPY of the current call's seen-set so a shared
            # callee already emitted in this call is not re-emitted (later
            # callers reference it by id in their calls field).
            trial_seen = set(cur_seen)
            section = self._render_tree(graph, depth_limit=self._max_depth, seen=trial_seen)

            # A member that cannot fit even alone (as the sole object in a call)
            # is depth-reduced / floored via build_individual — regardless of
            # what is already in the current call. Checked FIRST (and the
            # in-progress call flushed) so the outcome never depends on this
            # member's position in the batch.
            if self._limited and self._section_cost(section.text, first=True) > self._budget:
                flush()
                calls.extend(self.build_individual(mid))
                continue

            add_cost = self._section_cost(section.text, first=not cur_sections)
            if self._limited and cur_sections and cur_len + add_cost > self._budget:
                flush()
                trial_seen = set()
                section = self._render_tree(graph, depth_limit=self._max_depth, seen=trial_seen)
                add_cost = self._section_cost(section.text, first=True)

                if self._limited and add_cost > self._budget:
                    calls.extend(self.build_individual(mid))
                    continue

            cur_sections.append(section.text)
            cur_source_ids.extend(section.source_ids)
            cur_seen = trial_seen
            cur_len += add_cost

        flush()
        return calls

    # ── rendering ─────────────────────────────────────────────────────────

    def _render_tree(
        self,
        graph: dict,
        *,
        depth_limit: int,
        seen: Optional[set] = None,
    ) -> PackedCall:
        """Render a call-graph node tree to one JSON call-graph object.

        Every reachable node is emitted once as a function object whose
        ``status`` field records its state; call edges are carried by the
        ``calls`` field (array of callee ids), so hierarchy is explicit rather
        than implied by nesting. Repository source rides inside the ``source``
        JSON string (escaped), so it cannot forge structure. Node states:

          * already-described (in description_cache) → ``status`` "CACHED
            DESCRIPTION" with ``cached_description``; children still recursed
            (context only), not requested.
          * already emitted in this call (``seen``) → not re-emitted; callers
            still reference it by id in their ``calls`` field.
          * at/under ``depth_limit`` and uncached → ``status`` "SOURCE" with full
            ``source``, requested, children recursed.
          * past ``depth_limit`` → ``status`` "SIGNATURE" (``source`` is the
            one-line signature, empty ``calls``), not recursed, not requested
            (described on its own pass).
        """
        if seen is None:
            seen = set()
        nodes: List[_Node] = []
        source_ids: List[str] = []
        self._collect(graph, 0, depth_limit, seen, nodes, source_ids)
        return PackedCall(text=self._emit(graph["id"], nodes), source_ids=source_ids)

    def _collect(
        self,
        node: dict,
        depth: int,
        depth_limit: int,
        seen: set,
        nodes: List[_Node],
        source_ids: List[str],
    ) -> None:
        """Walk the tree depth-first, appending one ``_Node`` per unique node.

        The node's ``status`` is the single discriminator — ``_emit`` reads it to
        decide whether ``body`` is rendered as source or as a cached description.
        """
        fid = node["id"]
        if fid in seen:
            return  # already emitted in this call; referenced by id only
        seen.add(fid)
        ntype = node.get("type")
        child_ids = [c["id"] for c in node.get("calls", [])]

        # Already described: show the description as context, recurse for its
        # (possibly uncached) callees, but never request it.
        if fid in self._description_cache:
            nodes.append(
                _Node(fid, ntype, STATUS_CACHED, child_ids, self._description_cache[fid])
            )
            for child in node.get("calls", []):
                self._collect(child, depth + 1, depth_limit, seen, nodes, source_ids)
            return

        # Past the fitting depth: signature only, no recursion, no edges exposed
        # (its subtree is not shown in this call), not requested.
        if depth > depth_limit:
            nodes.append(_Node(fid, ntype, STATUS_SIGNATURE, [], self._signature(node)))
            return

        # Full SOURCE: a function we will ask the model to describe.
        nodes.append(_Node(fid, ntype, STATUS_SOURCE, child_ids, node["source"]))
        if ntype in _SUMMARIZABLE:
            source_ids.append(fid)
        for child in node.get("calls", []):
            self._collect(child, depth + 1, depth_limit, seen, nodes, source_ids)

    @staticmethod
    def _emit(root_id: str, nodes: List[_Node]) -> str:
        """Serialize collected nodes to one JSON call-graph object.

        Returns a single object ``{root, functions: [...]}``; callers wrap one or
        more of these in a JSON array to form a call's data block. ``body`` goes
        under ``cached_description`` for a cached node and ``source`` otherwise
        (SOURCE code or the one-line SIGNATURE) — ``status`` is the discriminator.
        """
        funcs: List[dict] = []
        for n in nodes:
            obj = {FIELD_ID: n.id, FIELD_TYPE: n.type, FIELD_STATUS: n.status, FIELD_CALLS: n.calls}
            obj[FIELD_CACHED if n.status == STATUS_CACHED else FIELD_SOURCE] = n.body
            funcs.append(obj)
        return json.dumps({FIELD_ROOT: root_id, FIELD_FUNCTIONS: funcs}, ensure_ascii=False)

    def _floor_call(self, graph: dict) -> PackedCall:
        """Last resort: even depth 0 overflows — emit only the entry point.

        The entry point is rendered as a SOURCE function object with its entire
        call tree dropped (``calls: []``); if its own body still does not fit, the
        body is truncated with a marker. Either way it still receives a (degraded)
        description rather than being dropped. ``source_truncated`` and
        ``tree_truncated`` record which fidelity loss(es) actually occurred so the
        caller can log them accurately — the marker is only emitted when the body
        was really cut. Returns the already-wrapped call (sized against the same
        budget as every other call).
        """
        fid = graph["id"]
        ntype = graph.get("type")
        original = graph["source"]
        # The floor always drops the call tree; so any direct callees are lost.
        tree_truncated = bool(graph.get("calls"))
        marker = "\n[... source truncated to fit the model context window ...]"

        def wrapped(src: str) -> str:
            obj = {FIELD_ROOT: fid, FIELD_FUNCTIONS: [
                {FIELD_ID: fid, FIELD_TYPE: ntype, FIELD_STATUS: STATUS_SOURCE,
                 FIELD_CALLS: [], FIELD_SOURCE: src}
            ]}
            return self._wrap([json.dumps(obj, ensure_ascii=False)])

        source = original
        source_truncated = False
        if self._limited and not self._fits(wrapped(original)):
            source_truncated = True
            # Shrink the retained prefix until the (marker-appended) call fits.
            # JSON escaping makes the serialized length non-linear in the raw
            # length, so shrink iteratively rather than compute a char offset.
            n = len(original)
            while n > 0 and not self._fits(wrapped(original[:n] + marker)):
                n -= max(1, n // 8)
            source = original[:max(0, n)] + marker
        src = [fid] if ntype in _SUMMARIZABLE else []
        return PackedCall(
            text=wrapped(source),
            source_ids=src,
            floored=True,
            source_truncated=source_truncated,
            tree_truncated=tree_truncated,
        )

    @staticmethod
    def _signature(node: dict) -> str:
        """One-line signature: the first non-blank line of the node's source."""
        for line in node.get("source", "").split("\n"):
            s = line.strip()
            if s:
                return s
        return node["id"]
