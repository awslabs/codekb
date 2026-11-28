"""Regression tests for component-id collision fixes.

Two distinct declarations that build the same id make ComponentCatalog.add_component
last-write-wins, silently dropping a component's source/calls/description/vector.
These tests pin the fixes that keep such declarations distinct:

  1. Overloaded methods/functions (param-type hash suffix, central pass).
  2. Kotlin extension functions and properties (receiver type in the id).
  3. Kotlin locals inside function bodies/lambdas (not emitted), and companion
     members (companion name in the id).
  4. C# method name read from the `name` field, not the return type.
  5. Terraform addresses prefixed per-file under V2 (unchanged under V1).
  6. ComponentCatalog counts overwrites in its statistics.
"""

import tempfile
from pathlib import Path


from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import DISAMBIG_SEP, IdScheme


def _analyze(files: dict[str, str], scheme: IdScheme = IdScheme.V2_PATH):
    """Write files into a temp dir, analyze it, return (catalog, components)."""
    d = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    catalog = ComponentCatalog()
    CodeAnalyzer(catalog).analyze_directory(d, space_id="t", id_scheme=scheme)
    return catalog, list(catalog._components.values())


def _methods(components, language):
    return [
        c
        for c in components
        if c.language == language and c.type.value in ("method", "function", "constructor")
    ]


def test_csharp_overloads_do_not_collide():
    catalog, comps = _analyze(
        {
            "svc.cs": """
class OrderService {
  public void Save(User u) {}
  public void Save(User u, bool force) {}
}
"""
        }
    )
    saves = [c for c in _methods(comps, "csharp") if c.get_simple_name() == "Save"]
    # Both overloads survive as distinct components...
    assert len(saves) == 2
    assert len({c.id for c in saves}) == 2
    # ...while the display / resolution name stays the bare method name.
    assert all(c.get_simple_name() == "Save" for c in saves)
    assert catalog.get_statistics()["overwritten_components"] == 0


def test_csharp_name_from_name_field_not_return_type():
    """`public Task Foo()` must be named Foo, not its return type Task."""
    _, comps = _analyze(
        {
            "svc.cs": """
class S {
  public Task Foo() { return null; }
  public Task Bar() { return null; }
}
"""
        }
    )
    names = {c.get_simple_name() for c in _methods(comps, "csharp")}
    assert "Foo" in names and "Bar" in names
    assert "Task" not in names


def test_csharp_properties_named_from_name_field_not_type():
    """Properties typed by a simple name must be named after the property, not
    the type: three `DateTime` properties are three components, not one."""
    _, comps = _analyze(
        {
            "svc.cs": """
interface IClock {
  DateTime CreatedAt { get; set; }
  DateTime UpdatedAt { get; set; }
  DateTime DeletedAt { get; set; }
}
"""
        }
    )
    props = [c for c in comps if c.language == "csharp" and c.type.value in ("variable", "constant")]
    names = {c.get_simple_name() for c in props}
    assert names == {"CreatedAt", "UpdatedAt", "DeletedAt"}
    assert len({c.id for c in props}) == 3  # distinct ids, no collapse
    assert "DateTime" not in names


def test_csharp_parameter_named_from_name_field_not_type():
    """A parameter typed by a simple name must be named after the parameter,
    not the type: `DateTime when` is the parameter `when`, not `DateTime`.
    Same root cause as the member-name fix; not gated, since a parameter name
    never forms a component id. Applies to V1 too (no refresh-stability risk)."""
    for scheme in (IdScheme.V2_PATH, IdScheme.V1_STEM):
        _, comps = _analyze(
            {"svc.cs": "class S { public void Schedule(DateTime when, int count, User owner) {} }"},
            scheme=scheme,
        )
        sched = next(c for c in _methods(comps, "csharp") if c.get_simple_name() == "Schedule")
        names = [p.name for p in sched.attributes.parameters]
        assert names == ["when", "count", "owner"], scheme


def test_kotlin_extension_functions_keep_receiver():
    _, comps = _analyze(
        {
            "ext.kt": """
fun String.toSlug(): String { return this }
fun Int.toSlug(): String { return "" }
"""
        }
    )
    slugs = [c for c in _methods(comps, "kotlin") if c.get_simple_name() == "toSlug"]
    assert len(slugs) == 2
    assert len({c.id for c in slugs}) == 2  # distinct ids via receiver type
    # The receiver appears in the id so the two are addressable apart.
    assert any("String" in c.id for c in slugs)
    assert any("Int" in c.id for c in slugs)


def test_kotlin_locals_are_not_emitted():
    _, comps = _analyze(
        {
            "a.kt": """
fun buildCart() { fun helper(): Int { return 1 }; val x = 5 }
fun buildOrder() { fun helper(): Int { return 2 } }
"""
        }
    )
    names = [c.get_simple_name() for c in _methods(comps, "kotlin")]
    assert "helper" not in names
    assert "buildCart" in names and "buildOrder" in names


def test_java_overloads_do_not_collide():
    """Java method overloads (same name, same class, different parameter lists)
    survive as distinct components under V2 via the param-signature hash suffix,
    while the display / resolution name stays the bare method name."""
    catalog, comps = _analyze(
        {
            "svc.java": """
class OrderService {
  public void save(User u) {}
  public void save(User u, boolean force) {}
  public void save(User u, boolean force, int retries) {}
}
"""
        }
    )
    saves = [c for c in _methods(comps, "java") if c.get_simple_name() == "save"]
    assert len(saves) == 3
    assert len({c.id for c in saves}) == 3
    assert all(c.id.rsplit(".", 1)[-1].startswith("save#") for c in saves)
    assert all(c.get_simple_name() == "save" for c in saves)
    assert catalog.get_statistics()["overwritten_components"] == 0


def test_kotlin_overloads_do_not_collide():
    """Kotlin method overloads survive as distinct components under V2. This is
    the reported case (a class with several getTemplate overloads kept only one
    before the central disambiguation pass ran for Kotlin)."""
    catalog, comps = _analyze(
        {
            "svc.kt": """
class TemplateService {
    fun getTemplate(id: String): String { return id }
    fun getTemplate(id: String, version: Int): String { return id }
    fun getTemplate(id: String, version: Int, locale: String): String { return id }
}
"""
        }
    )
    gets = [c for c in _methods(comps, "kotlin") if c.get_simple_name() == "getTemplate"]
    assert len(gets) == 3
    assert len({c.id for c in gets}) == 3
    assert all(c.id.rsplit(".", 1)[-1].startswith("getTemplate#") for c in gets)
    assert all(c.get_simple_name() == "getTemplate" for c in gets)
    assert catalog.get_statistics()["overwritten_components"] == 0


def test_typescript_overload_signatures_not_indexed():
    """TypeScript overloading is N type-only signatures plus exactly ONE
    implementation body (the compiler forbids a second body). The parser
    extracts only implementation nodes (`method_definition` /
    `function_declaration`) and skips signature-only nodes (`method_signature` /
    `function_signature`), so an overloaded name yields exactly one component —
    the implementation — and no collision occurs. This pins the current
    behaviour; indexing the overload signatures as their own components would be
    a separate TypeScript-parser change, after which they would flow through the
    same overload hash as the other languages."""
    catalog, comps = _analyze(
        {
            "svc.ts": """
export class TemplateService {
  getTemplate(id: string): string;
  getTemplate(id: string, version: number): string;
  getTemplate(id: string, version?: number): string { return id; }
}
"""
        }
    )
    gets = [c for c in _methods(comps, "typescript") if c.get_simple_name() == "getTemplate"]
    assert len(gets) == 1  # only the implementation body is indexed
    assert "#" not in gets[0].id  # single component, so no overload discriminator
    assert catalog.get_statistics()["overwritten_components"] == 0
def _properties(components, language):
    return [
        c for c in components if c.language == language and c.type.value in ("variable", "constant")
    ]


def test_kotlin_extension_property_does_not_overwrite_same_named_function():
    """`val Order.isEligible` must not replace `fun isEligible`, which holds the rule."""
    catalog, comps = _analyze(
        {
            "rules.kt": """
fun isEligible(order: Order): Boolean {
    return order.total > 100 && !order.customer.blocked
}

val Order.isEligible: Boolean
    get() = isEligible(this)
"""
        }
    )
    funcs = [c for c in _methods(comps, "kotlin") if c.get_simple_name() == "isEligible"]
    props = [c for c in _properties(comps, "kotlin") if c.get_simple_name() == "isEligible"]
    assert len(funcs) == 1 and len(props) == 1
    assert funcs[0].id != props[0].id
    assert props[0].id.endswith(".Order.isEligible")
    assert "Order" not in funcs[0].id
    assert catalog.get_statistics()["overwritten_components"] == 0


def test_kotlin_extension_properties_keep_receiver():
    _, comps = _analyze(
        {
            "ext.kt": """
val String.slug: String get() = lowercase()
val Int.slug: String get() = toString()
"""
        }
    )
    slugs = [c for c in _properties(comps, "kotlin") if c.get_simple_name() == "slug"]
    assert len(slugs) == 2
    assert len({c.id for c in slugs}) == 2
    assert any(c.id.endswith(".String.slug") for c in slugs)
    assert any(c.id.endswith(".Int.slug") for c in slugs)


def test_kotlin_extension_property_receiver_shapes_match_functions():
    """Generic and nullable receivers are written into the id as extension functions write them."""
    _, comps = _analyze(
        {
            "ext.kt": """
val <T> List<T>.second: T get() = this[1]
val String?.safeLen: Int get() = this?.length ?: 0
"""
        }
    )
    ids = {c.get_simple_name(): c.id for c in _properties(comps, "kotlin")}
    assert ids["second"].endswith(".List<T>.second")
    assert ids["safeLen"].endswith(".String?.safeLen")


def test_kotlin_plain_property_id_unchanged():
    """A dot inside the value or a delegate is not a receiver."""
    _, comps = _analyze(
        {
            "cfg.kt": """
val total: Int = order.amount.value
val cache by lazy { a.b }
"""
        }
    )
    ids = {c.get_simple_name(): c.id for c in _properties(comps, "kotlin")}
    # `>` closes the file's path hash, so nothing sits between the file and the name.
    assert ids["total"].endswith(">.total")
    assert ids["cache"].endswith(">.cache")


def _assert_property_function_collision_resolved(catalog, comps, language, name="bar"):
    """Shared assertions: a same-class property/field and function of one name
    both survive with distinct ids — the function keeps its bare id, the
    property-like member carries its kind as the discriminator — and nothing is
    overwritten."""
    methods = [c for c in _methods(comps, language) if c.get_simple_name() == name]
    props = [c for c in _properties(comps, language) if c.get_simple_name() == name]
    assert len(methods) == 1 and len(props) == 1
    assert methods[0].id != props[0].id
    assert DISAMBIG_SEP not in methods[0].id  # the function keeps its bare id
    # kind-derived (variable/constant), not hardcoded — a `val` parses as constant.
    assert props[0].id == f"{methods[0].id}{DISAMBIG_SEP}{props[0].type.value}"
    assert catalog.get_statistics()["overwritten_components"] == 0


def test_java_field_and_method_same_class_do_not_collide():
    catalog, comps = _analyze(
        {"Foo.java": "class Foo {\n  int bar;\n  int bar() { return bar; }\n}\n"}
    )
    _assert_property_function_collision_resolved(catalog, comps, "java")


def test_python_attr_and_method_same_class_do_not_collide():
    catalog, comps = _analyze(
        {"m.py": "class C:\n    bar = 1\n    def bar(self):\n        return 2\n"}
    )
    _assert_property_function_collision_resolved(catalog, comps, "python")


def test_kotlin_val_and_method_same_class_do_not_collide():
    catalog, comps = _analyze(
        {"Foo.kt": "class Foo {\n    val bar: Int = 1\n    fun bar(): Int { return 2 }\n}\n"}
    )
    _assert_property_function_collision_resolved(catalog, comps, "kotlin")


def test_property_function_collision_is_order_independent():
    """The two resulting ids do not depend on declaration order."""
    field_first = _analyze(
        {"Foo.java": "class Foo {\n  int bar;\n  int bar(){return 0;}\n}\n"}
    )[1]
    method_first = _analyze(
        {"Foo.java": "class Foo {\n  int bar(){return 0;}\n  int bar;\n}\n"}
    )[1]
    ids_a = {c.id for c in field_first if c.get_simple_name() == "bar"}
    ids_b = {c.id for c in method_first if c.get_simple_name() == "bar"}
    assert ids_a == ids_b and len(ids_a) == 2


def test_noncolliding_field_id_has_no_discriminator():
    """A field with no same-name sibling keeps its plain id — the kind tag is
    only appended on an actual collision."""
    _, comps = _analyze({"Foo.java": "class Foo {\n  int total;\n}\n"})
    total = next(c for c in comps if c.get_simple_name() == "total")
    assert DISAMBIG_SEP not in total.id


_KOTLIN_ORDER_WITH_COMPANION = """
class Order(val total: Int) {
    val label: String = "order"

    fun create(): Order {
        return Order(total * 2)
    }

    companion object {
        val label: String = "factory"

        fun create(): Order {
            return Order(0)
        }

        fun empty(): Order {
            return Order(0)
        }
    }
}

class Shop {
    fun open(): Order {
        return Order.empty()
    }
}
"""


def _after_file(component_id):
    """The id past the file's path hash: `t::Order<ab12>.Order.label` -> `.Order.label`."""
    return component_id.split(">", 1)[1]


def test_kotlin_companion_members_do_not_collide_with_class_members():
    catalog, comps = _analyze({"Order.kt": _KOTLIN_ORDER_WITH_COMPANION})
    order = next(c for c in comps if c.get_simple_name() == "Order" and c.type.value == "class")
    labels = [c for c in _properties(comps, "kotlin") if c.get_simple_name() == "label"]
    creates = [c for c in _methods(comps, "kotlin") if c.get_simple_name() == "create"]
    assert sorted(_after_file(c.id) for c in labels) == [".Order.Companion.label", ".Order.label"]
    # No `#` overload suffix: the two `create` no longer share an id.
    assert sorted(_after_file(c.id) for c in creates) == [
        ".Order.Companion.create",
        ".Order.create",
    ]
    # The parent stays the class, so calls like `Order.empty()` still resolve.
    assert all(c.parent_component == order.id for c in labels + creates)
    assert catalog.get_statistics()["overwritten_components"] == 0


def test_kotlin_named_companion_uses_its_name():
    _, comps = _analyze(
        {"a.kt": "class A {\n  companion object Factory {\n    fun make(): Int = 1\n  }\n}\n"}
    )
    make = next(c for c in _methods(comps, "kotlin") if c.get_simple_name() == "make")
    assert _after_file(make.id) == ".A.Factory.make"


def test_kotlin_call_to_companion_member_still_resolves():
    catalog, comps = _analyze({"Order.kt": _KOTLIN_ORDER_WITH_COMPANION})
    open_ = next(c for c in _methods(comps, "kotlin") if c.get_simple_name() == "open")
    empty = next(c for c in _methods(comps, "kotlin") if c.get_simple_name() == "empty")
    assert _after_file(empty.id) == ".Order.Companion.empty"
    assert _call_graph(catalog).get(open_.id) == [empty.id]


def test_kotlin_class_nested_in_companion_carries_companion():
    _, comps = _analyze(
        {
            "Order.kt": """
class Order {
    companion object {
        class Builder {
            fun build(): Int = 1
        }
    }
}
"""
        }
    )
    builder = next(c for c in comps if c.get_simple_name() == "Builder")
    build = next(c for c in _methods(comps, "kotlin") if c.get_simple_name() == "build")
    assert _after_file(builder.id) == ".Order.Companion.Builder"
    assert _after_file(build.id) == ".Order.Companion.Builder.build"


def test_terraform_same_address_distinct_across_stacks_v2():
    _, comps = _analyze(
        {
            "web/iam.tf": 'resource "aws_iam_role" "ecs_task_role" { name = "a" }\n',
            "worker/iam.tf": 'resource "aws_iam_role" "ecs_task_role" { name = "b" }\n',
        },
        scheme=IdScheme.V2_PATH,
    )
    roles = [c for c in comps if c.get_simple_name() == "aws_iam_role.ecs_task_role"]
    assert len(roles) == 2
    assert len({c.id for c in roles}) == 2  # prefixed per file → distinct
    # The name is the id; the address (the reference-resolution key) is read
    # back from the end of the id.
    assert all(c.name == c.id for c in roles)
    assert all(c.id.endswith(".aws_iam_role.ecs_task_role") for c in roles)


def test_terraform_v1_ids_unchanged():
    """Legacy V1 spaces keep address-only ids (and no separate display name)."""
    _, comps = _analyze(
        {"main.tf": 'resource "aws_s3_bucket" "logs" { bucket = "x" }\n'},
        scheme=IdScheme.V1_STEM,
    )
    ids = {c.id for c in comps if c.language == "terraform"}
    assert "t::aws_s3_bucket.logs" in ids  # verbatim address, no prefix


def test_cpp_overloads_do_not_collide_v2():
    _, comps = _analyze(
        {
            "a.cpp": (
                "class A { public:"
                " int f(int x){return x;}"
                " int f(int x,int y){return x;}"
                " void f(const char* s){} };"
            )
        },
        scheme=IdScheme.V2_PATH,
    )
    fs = [c for c in _methods(comps, "cpp") if c.get_simple_name() == "f"]
    assert len(fs) == 3
    assert len({c.id for c in fs}) == 3  # distinct via parameter-type signature


def test_cpp_prototype_and_definition_still_merge_v2():
    """A declaration and its definition share one id (same param types) and merge."""
    _, comps = _analyze(
        {"a.cpp": "class A { public: int f(int); };\nint A::f(int x){ return x; }\n"},
        scheme=IdScheme.V2_PATH,
    )
    fs = [c for c in _methods(comps, "cpp") if c.get_simple_name() == "f"]
    assert len(fs) == 1  # not duplicated


def test_cpp_v1_ids_have_no_discriminator():
    """Legacy V1 spaces keep bare ids (no parameter-signature suffix)."""
    _, comps = _analyze(
        {"a.cpp": "int add(int x){ return x; }\n"},
        scheme=IdScheme.V1_STEM,
    )
    add = next(c for c in _methods(comps, "cpp") if c.get_simple_name() == "add")
    assert add.id == "t::a.add"


def test_python_overloads_and_accessors_get_semantic_discriminator():
    """Python @overload stubs and property getter/setter share a name; they must
    survive as distinct components keyed on a real (parameter-bearing) signature,
    not an order-dependent positional suffix."""
    _, comps = _analyze(
        {
            "m.py": (
                "from typing import overload\n"
                "@overload\n"
                "def f(x: int) -> int: ...\n"
                "@overload\n"
                "def f(x: str) -> str: ...\n"
                "def f(x): return x\n"
                "class C:\n"
                "    @property\n"
                "    def val(self): return self._v\n"
                "    @val.setter\n"
                "    def val(self, v): self._v = v\n"
            )
        }
    )
    fs = [c for c in _methods(comps, "python") if c.get_simple_name() == "f"]
    vals = [c for c in _methods(comps, "python") if c.get_simple_name() == "val"]
    assert len(fs) == 3 and len({c.id for c in fs}) == 3
    assert len(vals) == 2 and len({c.id for c in vals}) == 2
    # Signatures are real (carry parameters), not the old bare "def".
    assert all(c.attributes.signature.startswith("def ") for c in fs)
    assert {c.attributes.signature for c in vals} == {"def val(self)", "def val(self, v)"}


def test_v1_legacy_behavior_is_unchanged():
    """All id-changing collision fixes are gated to V2; V1 spaces are untouched
    so delta-refresh keeps regenerating ids that match the persisted ones."""
    # C# overloads collapse under V1 (legacy last-write-wins), and the name is
    # still read as the return type.
    cat, comps = _analyze(
        {"s.cs": "class S { public Task Foo(){return null;} public void Foo(int x){} }"},
        scheme=IdScheme.V1_STEM,
    )
    cs = _methods(comps, "csharp")
    assert all("_" not in c.id.rsplit(".", 1)[-1] for c in cs)  # no overload hash suffix
    assert all("#" not in c.id for c in cs)
    assert any(c.get_simple_name() == "Task" for c in cs)  # legacy return-type name kept

    # C# properties typed by a simple name still collapse to the type under V1.
    _, pcomps = _analyze(
        {"c.cs": "interface I { DateTime A { get; set; } DateTime B { get; set; } }"},
        scheme=IdScheme.V1_STEM,
    )
    pnames = {c.get_simple_name() for c in pcomps if c.type.value in ("variable", "constant")}
    assert pnames == {"DateTime"}  # legacy type-name collapse kept

    # Kotlin: extension receiver NOT in the id, and locals ARE still emitted.
    _, kcomps = _analyze(
        {"e.kt": "fun String.toSlug():String{ fun helper():Int{return 1}; return this }"},
        scheme=IdScheme.V1_STEM,
    )
    knames = [c.get_simple_name() for c in _methods(kcomps, "kotlin")]
    assert "helper" in knames  # local kept under V1
    slug = next(c for c in _methods(kcomps, "kotlin") if c.get_simple_name() == "toSlug")
    assert "String" not in slug.id  # no receiver in the id under V1

    # Kotlin: extension property receiver NOT in the id either.
    _, kpcomps = _analyze(
        {"p.kt": "val String.slug: String get() = this"},
        scheme=IdScheme.V1_STEM,
    )
    pslug = next(c for c in _properties(kpcomps, "kotlin") if c.get_simple_name() == "slug")
    assert pslug.id == "t::p.slug"

    # Kotlin: companion NOT in the id either.
    _, kccomps = _analyze(
        {"a.kt": "class A {\n  companion object {\n    fun make(): Int = 1\n  }\n}\n"},
        scheme=IdScheme.V1_STEM,
    )
    make = next(c for c in _methods(kccomps, "kotlin") if c.get_simple_name() == "make")
    assert make.id == "t::a.A.make"


def test_cpp_default_value_decl_and_def_still_merge():
    """A C++ default argument is on the declaration, not the definition; the
    type signature must ignore it so the two still share an id and merge."""
    _, comps = _analyze(
        {"a.cpp": "class A { public: void f(int x = 5); };\nvoid A::f(int x) {}\n"},
        scheme=IdScheme.V2_PATH,
    )
    fs = [c for c in _methods(comps, "cpp") if c.get_simple_name() == "f"]
    assert len(fs) == 1  # merged, not duplicated


def test_cpp_disambiguator_widens_on_short_hash_collision():
    """Two DIFFERENT signatures that collide on the short hash must still get
    distinct ids (widened), while the SAME signature keeps one id (merge)."""
    import hashlib
    from src.parsers.cpp_parser import CppParser

    p = CppParser()
    p._cpp_disc_sigs = {}
    # Pre-seed the 4-char candidate for signature "int" with a DIFFERENT
    # signature, simulating a truncation collision.
    four = hashlib.sha1(b"int").hexdigest()[:4]
    p._cpp_disc_sigs[f"X.f#{four}"] = "OTHER_SIGNATURE"

    got = p._disambiguated_cpp_id("X.f", "int")
    assert got != f"X.f#{four}"          # did not reuse the taken short id
    assert got.startswith("X.f#")
    # Same signature again resolves to the same (widened) id -> decl/def merge.
    assert p._disambiguated_cpp_id("X.f", "int") == got


def test_overwrite_counter_resets_on_clear():
    """A catalog reused across runs reports only the current run's overwrites."""
    from src.core.models import Component, ComponentType

    cat = ComponentCatalog()
    for _ in range(2):
        cat.add_component(
            Component(id="t::d.X", type=ComponentType.CLASS, file_path=Path("a.py"),
                      line_number=1, end_line_number=2, language="python")
        )
    assert cat.get_statistics()["overwritten_components"] == 1
    cat.clear()
    assert cat.get_statistics()["overwritten_components"] == 0


def test_overwrite_counter_increments_on_collision():
    """A forced duplicate id is counted (defense for patterns not yet covered)."""
    from src.core.models import Component, ComponentType

    catalog = ComponentCatalog()
    for i in range(2):
        catalog.add_component(
            Component(
                id="t::dup.X",
                type=ComponentType.CLASS,
                file_path=Path("a.py"),
                line_number=1,
                end_line_number=2,
                language="python",
            )
        )
    assert catalog.get_statistics()["overwritten_components"] == 1


# ── name == id, and call resolution keyed on the id alone ───────────────────


def _call_graph(catalog):
    from src.flow.flow_analyzer import FlowAnalyzer

    return FlowAnalyzer(catalog).build_call_graph(catalog.get_all_components())


def test_v2_overload_ids_use_disambig_sep_and_name_is_id():
    _, comps = _analyze({"svc.cs": "class S { void Save(int a){} void Save(int a, bool b){} }"})
    saves = [c for c in _methods(comps, "csharp") if c.get_simple_name() == "Save"]
    assert len(saves) == 2
    assert all(c.id.rsplit(".", 1)[-1].startswith("Save#") for c in saves)
    assert all(c.name == c.id for c in comps)


def test_v2_call_to_overload_resolves_to_first_indexed():
    """Overloads stay distinct, and a call binds to exactly one of them."""
    catalog, comps = _analyze(
        {
            "svc.cs": "class S { void Save(int a){} void Save(int a, bool b){} void Run(){ Save(1); } }"
        }
    )
    run = next(c for c in comps if c.get_simple_name() == "Run")
    saves = {c.id for c in comps if c.get_simple_name() == "Save"}
    targets = _call_graph(catalog).get(run.id, [])
    assert len(targets) == 1 and targets[0] in saves


def test_v2_cpp_calls_resolve():
    catalog, comps = _analyze(
        {"a.cpp": "int add(int x){ return x; }\nint run(){ return add(1); }\n"}
    )
    add = next(c for c in _methods(comps, "cpp") if c.get_simple_name() == "add")
    run = next(c for c in _methods(comps, "cpp") if c.get_simple_name() == "run")
    assert "#" in add.id
    assert _call_graph(catalog).get(run.id) == [add.id]


def test_v2_terraform_references_resolve():
    catalog, comps = _analyze(
        {
            "main.tf": (
                'variable "region" {}\n'
                'resource "aws_iam_role" "ecs_task_role" { name = var.region }\n'
                'resource "aws_ecs_task_definition" "t" {'
                " task_role_arn = aws_iam_role.ecs_task_role.arn }\n"
            )
        }
    )
    by_address = {c.get_simple_name(): c.id for c in comps if c.language == "terraform"}
    graph = _call_graph(catalog)
    assert graph[by_address["aws_ecs_task_definition.t"]] == [
        by_address["aws_iam_role.ecs_task_role"]
    ]
    assert graph[by_address["aws_iam_role.ecs_task_role"]] == [by_address["var.region"]]


def test_v1_terraform_references_resolve():
    """V1 ids are space-qualified addresses; references still resolve against them."""
    catalog, _ = _analyze(
        {
            "main.tf": (
                'variable "region" {}\n'
                'resource "aws_iam_role" "ecs_task_role" { name = var.region }\n'
            )
        },
        scheme=IdScheme.V1_STEM,
    )
    assert _call_graph(catalog)["t::aws_iam_role.ecs_task_role"] == ["t::var.region"]


def test_qualified_search_pattern_matches_v2_terraform():
    catalog, _ = _analyze({"main.tf": 'resource "aws_iam_role" "ecs_task_role" { name = "a" }\n'})
    hits = catalog.search_components("t::*ecs_task_role*")
    assert [c.get_simple_name() for c in hits] == ["aws_iam_role.ecs_task_role"]
