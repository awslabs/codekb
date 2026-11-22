"""Unit tests for the token-aware summarization input packer.

Synthetic call-graph dicts are injected via `fetch_graph`, so these exercise the
fitting logic with no files, service, or Bedrock. The packer's budget is the
character ceiling `max_input_chars`, so `budget_chars` maps directly to the limit.
"""

from src.vector.summarization_packer import SummarizationPacker


def _node(cid, src, *calls, ntype="function"):
    return {"id": cid, "type": ntype, "source": src, "calls": list(calls)}


def _packer(trees, budget_chars, *, cache=None, max_depth=10):
    return SummarizationPacker(
        fetch_graph=lambda cid, depth: trees.get(cid),
        description_cache=cache if cache is not None else {},
        max_depth=max_depth,
        max_input_chars=budget_chars,
    )


# A 3-level tree: entry -> mid -> leaf.
def _three_level():
    leaf = _node("leaf", "def leaf(): return 1")
    mid = _node("mid", "def mid(): return leaf()", leaf)
    entry = _node("entry", "def entry(): return mid()", mid)
    return {"entry": entry}


def test_full_depth_fits_renders_everything_as_source():
    trees = _three_level()
    calls = _packer(trees, 10_000).build_individual("entry")
    assert len(calls) == 1
    call = calls[0]
    assert set(call.source_ids) == {"entry", "mid", "leaf"}
    assert '"status": "SIGNATURE"' not in call.text
    assert not call.floored


def test_tight_budget_drops_deepest_to_signature():
    # A multi-line leaf so its SOURCE body is clearly larger than its one-line
    # SIGNATURE — otherwise reducing depth would not shrink the render.
    leaf = _node("leaf", "def leaf():\n" + "    total += step\n" * 25 + "    return total")
    mid = _node("mid", "def mid(): return leaf()", leaf)
    entry = _node("entry", "def entry(): return mid()", mid)
    trees = {"entry": entry}

    # Measure the full and depth-1 renders; a budget at the depth-1 object size
    # forces the leaf to drop to a signature while entry/mid stay SOURCE.
    p = _packer(trees, 0)
    full_obj = p._render_tree(entry, depth_limit=10).text
    d1_obj = p._render_tree(entry, depth_limit=1).text
    assert len(d1_obj) < len(full_obj)
    call = _packer(trees, len(d1_obj)).build_individual("entry")[0]

    # The deepest node is no longer requested, and appears as a signature.
    assert "leaf" not in call.source_ids
    assert "entry" in call.source_ids
    assert '"status": "SIGNATURE"' in call.text
    # Invariant: every requested id is rendered as a SOURCE function object.
    import json
    by_id = {f["id"]: f for o in json.loads(call.text) for f in o["functions"]}
    for fid in call.source_ids:
        assert by_id[fid]["status"] == "SOURCE" and "source" in by_id[fid]


def test_floor_truncates_single_oversized_function():
    big = _node("entry", "x" * 5000)
    call = _packer({"entry": big}, 500).build_individual("entry")[0]
    assert call.floored
    assert "truncated to fit" in call.text
    assert call.source_ids == ["entry"]
    assert "x" in call.text                 # a partial body survives
    assert len(call.text) <= 500            # and stays within budget (incl. indentation)


def test_floor_within_budget_for_a_single_long_line():
    # One line longer than the budget must still truncate within budget.
    one_line = _node("entry", "Z" * 5000)
    call = _packer({"entry": one_line}, 500).build_individual("entry")[0]
    assert call.floored
    assert "Z" in call.text
    assert len(call.text) <= 500


def test_oversized_batch_member_is_floored_regardless_of_order():
    small = _node("a", "def a(): pass")
    huge = _node("b", "B" * 4000)  # exceeds budget alone
    trees = {"a": small, "b": huge}
    for order in (["a", "b"], ["b", "a"]):
        calls = _packer(trees, 500).build_batch(order)
        # No call is ever over budget, and b is floored either way.
        assert all(len(c.text) <= 500 for c in calls)
        b_calls = [c for c in calls if c.source_ids == ["b"]]
        assert len(b_calls) == 1 and b_calls[0].floored


def test_batch_packs_all_members_when_they_fit():
    trees = {
        "a": _node("a", "def a(): pass"),
        "b": _node("b", "def b(): pass"),
    }
    calls = _packer(trees, 10_000).build_batch(["a", "b"])
    assert len(calls) == 1
    assert set(calls[0].source_ids) == {"a", "b"}


def test_batch_splits_when_over_budget_without_dropping_members():
    a = _node("a", "A" * 300)
    b = _node("b", "B" * 300)
    trees = {"a": a, "b": b}
    # Size of a SINGLE member's rendered section.
    single = _packer(trees, 10_000).build_batch(["a"])[0].text
    # Budget holds one member comfortably but not a second ~300-char member.
    calls = _packer(trees, len(single) + 100).build_batch(["a", "b"])
    assert len(calls) == 2
    # Every member still described across the calls — nothing dropped.
    assert {i for c in calls for i in c.source_ids} == {"a", "b"}


def test_shared_callee_rendered_once_per_batch_call():
    shared = _node("shared", "def shared(): return 42")
    a = _node("a", "def a(): return shared()", shared)
    b = _node("b", "def b(): return shared()", shared)
    trees = {"a": a, "b": b}
    calls = _packer(trees, 100_000).build_batch(["a", "b"])
    assert len(calls) == 1
    text = calls[0].text
    # shared is emitted once as a single function object with its body...
    assert text.count("def shared(): return 42") == 1
    assert text.count('"id": "shared"') == 1
    # ...and both members reference it by id in their calls array.
    assert text.count('["shared"]') == 2
    # And it is requested only once.
    assert calls[0].source_ids.count("shared") == 1


def test_output_is_json_array_of_graph_objects():
    import json

    trees = _three_level()
    text = _packer(trees, 10_000).build_individual("entry")[0].text
    # A call's data block is a JSON array of call-graph objects; here, one object
    # with a flat functions list carrying edges in each node's "calls" array.
    parsed = json.loads(text)
    assert isinstance(parsed, list) and len(parsed) == 1
    obj = parsed[0]
    assert obj["root"] == "entry"
    by_id = {f["id"]: f for f in obj["functions"]}
    assert by_id["entry"]["status"] == "SOURCE" and by_id["entry"]["calls"] == ["mid"]
    assert by_id["mid"]["calls"] == ["leaf"]
    assert by_id["leaf"]["calls"] == []
    assert "source" in by_id["leaf"]
    # No remnants of the old XML/indented format.
    assert "<call_graph" not in text and "<function" not in text
    assert "Call Graph for:" not in text


def test_cached_callee_is_context_not_requested():
    trees = _three_level()
    calls = _packer(trees, 10_000, cache={"leaf": "Returns one."}).build_individual("entry")
    call = calls[0]
    assert '"status": "CACHED DESCRIPTION"' in call.text
    assert "Returns one." in call.text
    assert "leaf" not in call.source_ids  # cached → context only, never requested


def test_cached_node_source_is_fully_masked_and_children_kept():
    """A cached function is replaced by its description (no source leak), but its
    own callees are still rendered as context."""
    leaf = _node("leaf", "def leaf():\n    LEAK = 1\n    return LEAK")
    mid = _node("mid", "def mid():\n\n    MID_BODY = leaf()", leaf)
    entry = _node("entry", "def entry(): return mid()", mid)
    calls = _packer({"entry": entry}, 100_000, cache={"mid": "Calls leaf."}).build_individual(
        "entry"
    )
    text = calls[0].text
    assert "Calls leaf." in text          # cached description shown
    assert "MID_BODY" not in text         # cached node's own source masked
    assert '"id": "leaf"' in text         # child of the cached node still rendered
    assert "def leaf():" in text          # ...with its source (it is uncached)
    assert "mid" not in calls[0].source_ids   # cached node not requested
    assert "leaf" in calls[0].source_ids      # its uncached child is
