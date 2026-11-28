"""Runnable harness for the property/function id-collision work.

Directly calls CodeAnalyzer.analyze_directory (no server, no container) on small
in-memory examples and checks invariants, so the same script demonstrates the
as-is bug and then verifies the fix. Named harness_* (not test_*) so pytest does
not collect it; run it directly:

    backend/.venv/bin/python -m test.harness_prop_func_collision      # from backend/
    # or
    cd backend && .venv/bin/python test/harness_prop_func_collision.py

Exit code 0 = all scenarios PASS, 1 = at least one FAIL.
"""

import logging
import sys
import tempfile
from pathlib import Path

logging.disable(logging.CRITICAL)  # silence analyzer logging for a clean report

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import IdScheme, DISAMBIG_SEP


def analyze(files: dict[str, str], scheme: IdScheme = IdScheme.V2_PATH):
    """Write files to a temp dir, analyze, return (catalog, surviving components)."""
    d = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    catalog = ComponentCatalog()
    CodeAnalyzer(catalog).analyze_directory(d, space_id="t", id_scheme=scheme)
    return catalog, list(catalog._components.values())


def survivors_named(comps, name):
    return [c for c in comps if c.get_simple_name() == name]


def report(title, comps, overwritten):
    print(f"\n===== {title} =====")
    for c in sorted(comps, key=lambda c: c.id):
        print(f"  [{c.type.value:11}] {c.id}")
    print(f"  overwritten_components = {overwritten}")


# ── Scenarios ──────────────────────────────────────────────────────────────
# Each scenario: analyze files, print the ids, evaluate an invariant.
# "expect_distinct_pair": a name that should resolve to exactly two surviving
#   components (one method, one property-like) with distinct ids and no overwrite.
# "expect_overloads": a name that should resolve to N distinct methods.
# "cross_file"/"order": special checks.

FAILURES = []


def check(cond, msg):
    status = "PASS" if cond else "FAIL"
    print(f"    [{status}] {msg}")
    if not cond:
        FAILURES.append(msg)


def scenario_property_vs_method(lang, filename, source, name="bar"):
    cat, comps = analyze({filename: source})
    report(f"{lang}: property/field '{name}' + method '{name}()' in one class", comps,
           cat.get_statistics()["overwritten_components"])
    group = survivors_named(comps, name)
    types = sorted(c.type.value for c in group)
    ids = {c.id for c in group}
    check(len(group) == 2, f"{lang}: both '{name}' components survive (got {len(group)})")
    check(len(ids) == len(group), f"{lang}: surviving '{name}' ids are distinct")
    check(cat.get_statistics()["overwritten_components"] == 0,
          f"{lang}: no components overwritten")
    methods = [c for c in group if c.type.value in ("method", "function", "constructor")]
    props = [c for c in group if c.type.value in ("variable", "constant")]
    check(len(methods) == 1 and len(props) == 1,
          f"{lang}: exactly one method and one property-like survive (types={types})")
    if methods and props:
        check(DISAMBIG_SEP not in methods[0].id,
              f"{lang}: the function keeps its bare id")
        check(props[0].id == f"{methods[0].id}{DISAMBIG_SEP}{props[0].type.value}",
              f"{lang}: the property id is function_id#<kind> ({props[0].id.split(DISAMBIG_SEP)[-1]})")


def scenario_overloads():
    cat, comps = analyze(
        {"Calc.java": "class Calc {\n  int add(int a){return a;}\n"
                      "  int add(int a,int b){return a+b;}\n"
                      "  int add(int a,int b,int c){return a+b+c;}\n}\n"}
    )
    report("Java: add() overloads (must stay distinct — regression)", comps,
           cat.get_statistics()["overwritten_components"])
    adds = survivors_named(comps, "add")
    check(len(adds) == 3, f"overloads: all 3 add() survive (got {len(adds)})")
    check(len({c.id for c in adds}) == 3, "overloads: the 3 ids are distinct")
    check(all(DISAMBIG_SEP in c.id for c in adds), "overloads: each carries a #signature suffix")
    check(cat.get_statistics()["overwritten_components"] == 0, "overloads: no overwrite")


def scenario_cross_file():
    cat, comps = analyze({
        "a/util.java": "class util {\n  int FOO;\n  int helper(){return 1;}\n}\n",
        "b/util.java": "class util {\n  int FOO;\n  int helper(){return 2;}\n}\n",
    })
    report("Cross-file: same names in a/util.java and b/util.java (regression)", comps,
           cat.get_statistics()["overwritten_components"])
    foos = survivors_named(comps, "FOO")
    helpers = survivors_named(comps, "helper")
    check(len(foos) == 2 and len({c.id for c in foos}) == 2,
          f"cross-file: both FOO fields survive distinctly (got {len(foos)})")
    check(len(helpers) == 2 and len({c.id for c in helpers}) == 2,
          f"cross-file: both helper methods survive distinctly (got {len(helpers)})")
    check(cat.get_statistics()["overwritten_components"] == 0, "cross-file: no overwrite")


def scenario_order_independence():
    a = analyze({"Foo.java": "class Foo {\n  int bar;\n  int bar(){return 0;}\n}\n"})[1]
    b = analyze({"Foo.java": "class Foo {\n  int bar(){return 0;}\n  int bar;\n}\n"})[1]
    ids_a = {c.id for c in survivors_named(a, "bar")}
    ids_b = {c.id for c in survivors_named(b, "bar")}
    print("\n===== Order-independence: field/method declared in either order =====")
    print(f"  order A ids: {sorted(ids_a)}")
    print(f"  order B ids: {sorted(ids_b)}")
    check(ids_a == ids_b and len(ids_a) == 2,
          "order-independence: same two ids regardless of declaration order")


def main():
    print("#" * 78)
    print("# property/function id-collision harness (direct CodeAnalyzer calls, V2)")
    print("#" * 78)

    scenario_property_vs_method(
        "Java", "Foo.java", "class Foo {\n  int bar;\n  int bar() { return bar; }\n}\n")
    scenario_property_vs_method(
        "Python", "m.py", "class C:\n    bar = 1\n    def bar(self):\n        return 2\n", name="bar")
    scenario_property_vs_method(
        "Kotlin", "Foo.kt", "class Foo {\n    val bar: Int = 1\n    fun bar(): Int { return 2 }\n}\n")
    scenario_overloads()
    scenario_cross_file()
    scenario_order_independence()

    print("\n" + "#" * 78)
    if FAILURES:
        print(f"# RESULT: {len(FAILURES)} check(s) FAILED")
        for m in FAILURES:
            print(f"   - {m}")
        return 1
    print("# RESULT: all checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
