"""Tests for implicit annotation-handler edges (issue #44).

Covers the pointcut shape allowlist, the Spring adapter (accepted shapes,
refusals, target-side exclusions, name resolution), the language-agnostic
core contract, and how the edges surface: dependency tools, trace flow,
summarization input, refresh staleness and backward compatibility.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.analysis.content_hash import backfill_component_hashes
from src.analysis.orchestrator import AnalysisOrchestrator
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import (
    EDGE_KIND_ANNOTATION,
    BusinessBehavior,
    CallType,
    ChangeType,
    Component,
    ComponentType,
    DependencyAnalysis,
    FileChange,
    FlowStep,
    KnowledgeBase,
    call_edge_kind,
    call_edge_target,
)
from src.flow import annotation_edges
from src.flow.flow_analyzer import FlowAnalyzer
from src.flow.pointcut_shapes import (
    ANNOTATION,
    EXECUTION_ANNOTATED,
    REFERENCE,
    WITHIN,
    Leaf,
    parse_pointcut,
)
from src.mcp.tools.flow_tools import _format_call_graph, _format_dependencies, _format_flow
from src.vector.topdown_summarizer import TopDownSummarizer

# ── fixture sources ──────────────────────────────────────────────────────

POLICY_CHECK = """package com.example.annot;

import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;

@Retention(RetentionPolicy.RUNTIME)
public @interface PolicyCheck {
    String value() default "";
}
"""

TRANSFER_SERVICE = """package com.example.pay;

import com.example.annot.PolicyCheck;

public class TransferService {
    @PolicyCheck("P-17")
    public String transfer(String req) {
        return post(req);
    }

    String post(String req) {
        return req;
    }
}
"""

LEDGER_SERVICE = """package com.example.pay;

import com.example.annot.PolicyCheck;

@PolicyCheck("P-9")
public class LedgerService {
    public String settle(String req) {
        return req;
    }

    private String hidden(String req) {
        return req;
    }

    static String helper(String req) {
        return req;
    }
}
"""

# Binding-form @annotation advice plus a @within advice, as in the issue.
POLICY_ASPECT = """package com.example.aspect;

import com.example.annot.PolicyCheck;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;

@Aspect
public class PolicyAspect {
    @Around("@annotation(policyCheck)")
    public Object apply(ProceedingJoinPoint pjp, PolicyCheck policyCheck) throws Throwable {
        return pjp.proceed();
    }

    @Before("@within(com.example.annot.PolicyCheck)")
    public void applyToType() {
    }
}
"""

BASE_FILES = {
    "annot/PolicyCheck.java": POLICY_CHECK,
    "pay/TransferService.java": TRANSFER_SERVICE,
    "pay/LedgerService.java": LEDGER_SERVICE,
    "aspect/PolicyAspect.java": POLICY_ASPECT,
}

FQN = "com.example.annot.PolicyCheck"


def _aspect(advice: str, params: str = "", *, header: str = "@Aspect\npublic class PolicyAspect",
            members: str = "", imports: str = "") -> str:  # fmt: skip
    """An aspect with one advice method ``advise``."""
    return f"""package com.example.aspect;

import com.example.annot.PolicyCheck;
import org.aspectj.lang.JoinPoint;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.After;
import org.aspectj.lang.annotation.AfterReturning;
import org.aspectj.lang.annotation.AfterThrowing;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;
import org.aspectj.lang.annotation.Pointcut;
{imports}
{header} {{
{members}
    {advice}
    public void advise({params}) {{
    }}
}}
"""


def _analyze(root: Path, files: dict[str, str], *, reverse: bool = False):
    """Write files, analyze, and build the call graph like the service does."""
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    kb = CodeAnalyzer(ComponentCatalog(None)).analyze_directory(root, "space")
    catalog = ComponentCatalog(None)
    components = sorted(kb.components, key=lambda c: str(c.file_path), reverse=reverse)
    for comp in components:
        catalog.add_component(comp)
    analyzer = FlowAnalyzer(catalog)
    analyzer.build_call_graph(components)
    return kb, analyzer


def _cid(kb: KnowledgeBase, suffix: str) -> str:
    matches = [c.id for c in kb.components if c.id.endswith(suffix)]
    assert len(matches) == 1, f"{suffix!r} matched {matches}"
    return matches[0]


def _annotation_edges(analyzer: FlowAnalyzer) -> dict[str, list[str]]:
    """caller id -> sorted handler ids over implicit annotation edges only."""
    result: dict[str, list[str]] = {}
    for caller, calls in analyzer._call_graph.items():
        handlers = sorted(
            call_edge_target(e) for e in calls if call_edge_kind(e) == EDGE_KIND_ANNOTATION
        )
        if handlers:
            result[caller] = handlers
    return result


def _handlers(tmp_path: Path, files: dict[str, str], suffix: str) -> list[str]:
    """Handler method names (``Class.method``) linked from the method ending in suffix."""
    kb, analyzer = _analyze(tmp_path, files)
    method = next((c.id for c in kb.components if c.id.endswith(suffix)), None)
    return [
        h.rsplit("_", 1)[-1].split(".", 1)[-1] for h in _annotation_edges(analyzer).get(method, [])
    ]


def _links(tmp_path: Path, files: dict[str, str], suffix: str = "TransferService.transfer") -> bool:
    return bool(_handlers(tmp_path, files, suffix))


def _with_aspect(aspect: str, **extra: str) -> dict[str, str]:
    return {**BASE_FILES, "aspect/PolicyAspect.java": aspect, **extra}


# ── pointcut shapes ──────────────────────────────────────────────────────


class TestPointcutShapes:
    @pytest.mark.parametrize(
        "expression,leaves",
        [
            (f"@annotation({FQN})", [(ANNOTATION, FQN)]),
            ("@annotation(check)", [(ANNOTATION, "check")]),
            (f" @within( {FQN} ) ", [(WITHIN, FQN)]),
            (f"execution(@{FQN} * *(..))", [(EXECUTION_ANNOTATED, FQN)]),
            ("checked()", [(REFERENCE, "checked")]),
            ("@annotation(a.B)|| @within(a.B) ||\n\tchecked( )",
             [(ANNOTATION, "a.B"), (WITHIN, "a.B"), (REFERENCE, "checked")]),
            ("@annotation(com.notify.and.Audit)", [(ANNOTATION, "com.notify.and.Audit")]),
        ],
    )  # fmt: skip
    def test_accepted(self, expression, leaves):
        assert parse_pointcut(expression) == tuple(Leaf(*leaf) for leaf in leaves)

    @pytest.mark.parametrize(
        "expression",
        [
            "", "!@annotation(a.B)", "@annotation(a.B) && execution(* *(..))",
            "@annotation(a.B) and execution(* *(..))", "@annotation(a.B) or @annotation(a.C)",
            "not @annotation(a.B)", "@annotation(not)", "(@annotation(a.B))", "@annotation(a.B) ||",
            "|| @annotation(a.B)", "@annotation(a.*)", "@target(a.B)", "within(a..*)",
            "execution(* *(..))", "execution(@a.B public * *(..))", "execution()", "if()",
            "Other.checked()", "checked(x)", "@annotation(a.B)\f", "@annotation(a.B) ",
            "@annotation(a.B) # c", "@annotation(a.é)",
        ],
    )  # fmt: skip
    def test_refused(self, expression):
        assert parse_pointcut(expression) is None


# ── annotation extraction into decorators ────────────────────────────────


class TestJavaAnnotationExtraction:
    def test_annotations_populate_decorators(self, tmp_path):
        kb, _ = _analyze(tmp_path, BASE_FILES)
        by_id = {c.id: c for c in kb.components}
        assert by_id[_cid(kb, "TransferService.transfer")].attributes.decorators == ["@PolicyCheck"]
        assert by_id[_cid(kb, ".LedgerService")].attributes.decorators == ["@PolicyCheck"]
        assert by_id[_cid(kb, ".PolicyAspect")].attributes.decorators == ["@Aspect"]
        assert by_id[_cid(kb, "PolicyAspect.apply")].attributes.decorators == ["@Around"]
        assert by_id[_cid(kb, "TransferService.post")].attributes.decorators == []


# ── the Spring adapter: accepted shapes ──────────────────────────────────


class TestAcceptedShapes:
    def test_binding_form_and_within_as_in_the_issue(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        edges = _annotation_edges(analyzer)
        transfer = _cid(kb, "TransferService.transfer")
        assert edges[transfer] == [_cid(kb, "PolicyAspect.apply")]
        # The edge records the annotation usage's line and the edge kind.
        assert (_cid(kb, "PolicyAspect.apply"), 6, EDGE_KIND_ANNOTATION) in analyzer._call_graph[
            transfer
        ]
        # @within: every advisable method of the annotated class, at the class
        # annotation's line; private and static methods are never advised.
        settle = _cid(kb, "LedgerService.settle")
        assert edges[settle] == [_cid(kb, "PolicyAspect.applyToType")]
        assert (_cid(kb, "PolicyAspect.applyToType"), 5, EDGE_KIND_ANNOTATION) in (
            analyzer._call_graph[settle]
        )
        assert set(edges) == {transfer, settle}

    @pytest.mark.parametrize(
        "advice,params",
        [
            (f'@Before("@annotation({FQN})")', ""),
            (f'@Before("execution(@{FQN} * *(..))")', ""),
            ('@Before("@annotation(check)")', "PolicyCheck check"),
            ('@Before("@annotation(check)")', "JoinPoint jp, PolicyCheck check"),
            ('@Before("@annotation(check)")', "JoinPoint.StaticPart sp, PolicyCheck check"),
            ('@Around("@annotation(check)")', "ProceedingJoinPoint pjp, PolicyCheck check"),
            ('@AfterReturning(pointcut = "@annotation(check)", returning = "result")',
             "JoinPoint jp, PolicyCheck check, Object result"),
            ('@AfterThrowing(value = "@annotation(check)", throwing = "e")',
             "PolicyCheck check, Exception e"),
            (f'@AfterReturning(pointcut = "@annotation({FQN})", returning = "r")', "java.lang.Object r"),
            ('@Before(value = "@annotation(c)", argNames = "c")', "PolicyCheck check"),
            ('@Before(value = "@annotation(c)", argNames = "jp, c")', "JoinPoint jp, PolicyCheck check"),
            (f'@After("@annotation(com.example.annot.Other) || @annotation({FQN})")', ""),
            (f'@Before("@annotation(com.lib.Audited) || @annotation({FQN})")', ""),
            (f'@Before("@annotation(" + "{FQN})" + "|| @within({FQN})")', ""),
            ('@Before("checked()")', ""),
            ('@Before("viaOther()")', ""),
        ],
    )  # fmt: skip
    def test_shape_links(self, tmp_path, advice, params):
        members = (
            f'    @Pointcut("@annotation({FQN})")\n    public void checked() {{\n    }}\n\n'
            '    @Pointcut("checked()")\n    void viaOther() {\n    }\n'
        )
        other = POLICY_CHECK.replace("PolicyCheck", "Other")
        files = _with_aspect(
            _aspect(advice, params, members=members), **{"annot/Other.java": other}
        )
        assert _handlers(tmp_path, files, "TransferService.transfer") == ["PolicyAspect.advise"]

    @pytest.mark.parametrize(
        "header",
        ['@Aspect("")\npublic class PolicyAspect',
         "@Aspect\npublic class PolicyAspect implements org.springframework.core.Ordered",
         "@org.aspectj.lang.annotation.Aspect\nclass PolicyAspect"],
    )  # fmt: skip
    def test_aspect_forms_that_link(self, tmp_path, header):
        aspect = _aspect(f'@Before("@annotation({FQN})")', header=header)
        assert _links(tmp_path, _with_aspect(aspect))

    def test_one_edge_per_handler_and_several_aspects_each_link(self, tmp_path):
        files = dict(BASE_FILES)
        # The class and the method both carry the annotation: one edge per handler.
        files["pay/TransferService.java"] = TRANSFER_SERVICE.replace(
            "public class TransferService", '@PolicyCheck("C")\npublic class TransferService'
        )
        files["aspect/AuditAspect.java"] = _aspect(f'@Before("@annotation({FQN})")').replace(
            "PolicyAspect", "AuditAspect"
        )
        kb, analyzer = _analyze(tmp_path, files)
        transfer = _cid(kb, "TransferService.transfer")
        handlers = [call_edge_target(e) for e in analyzer._call_graph[transfer] if len(e) > 2]
        assert sorted(handlers) == sorted(
            _cid(kb, s)
            for s in ("AuditAspect.advise", "PolicyAspect.apply", "PolicyAspect.applyToType")
        )

    def test_edges_are_deterministic_across_component_order(self, tmp_path):
        _, forward = _analyze(tmp_path / "a", BASE_FILES)
        _, backward = _analyze(tmp_path / "b", BASE_FILES, reverse=True)
        assert _annotation_edges(forward) == _annotation_edges(backward)


# ── the Spring adapter: refusals ─────────────────────────────────────────


class TestRefusedShapes:
    @pytest.mark.parametrize(
        "advice,params",
        [
            (f'@Before("!@annotation({FQN})")', ""),
            (f'@Before("@annotation({FQN}) && execution(* *(..))")', ""),
            (f'@Before("execution(* *(..)) and @annotation({FQN})")', ""),
            (f'@Before("({FQN})")', ""),
            (f'@Before("@annotation(PolicyCheck) || @annotation({FQN})")', ""),  # a simple type name
            (f'@Before("@annotation({FQN}) || within(com.example..*)")', ""),
            (f'@Before("@annotation(com.example.pay.TransferService) || @annotation({FQN})")', ""),
            ('@Before("Other.checked()")', ""),
            ('@Before("undefined()")', ""),
            ('@Before("\\u0040annotation(com.example.annot.PolicyCheck)")', ""),
            ('@Before("@annotation(com.example.annot.PolicyCheck)\\t")', ""),
            (f'@Before("""\n        @annotation({FQN})""")', ""),
            ("@Before(PC)", ""),
            ('@Before(PC + "")', ""),
            (f'@AfterReturning(value = "@annotation({FQN})", pointcut = "@annotation({FQN})")', ""),
            # Advice parameters the pointcut does not bind, or binds otherwise.
            (f'@Before("@annotation({FQN})")', "String s"),
            ('@Before("@annotation(check)")', "ProceedingJoinPoint pjp, PolicyCheck check"),
            ('@Before("@annotation(check)")', "PolicyCheck check, PolicyCheck other"),
            (f'@Before("@annotation(check) || @annotation({FQN})")', "PolicyCheck check"),
            ('@Before("@within(check)")', "PolicyCheck check"),
            ('@Before("@annotation(check)")', "String check"),
            ('@AfterReturning(pointcut = "@annotation(check)", returning = "r")', "PolicyCheck check"),
            ('@Before(value = "@annotation(check)", argNames = "a, b")', "PolicyCheck check"),
            # A returning type narrower than Object: Spring skips other return values.
            (f'@AfterReturning(pointcut = "@annotation({FQN})", returning = "r")', "String r"),
            (f'@AfterReturning(pointcut = "@annotation({FQN})", returning = "r")', "int r"),
            # Several AspectJ annotations on one method.
            (f'@Before("@annotation({FQN})") @After("@annotation({FQN})")', ""),
            # Named pointcuts: one with parameters, a cycle.
            ('@Before("withParam()")', ""),
            ('@Before("loop()")', ""),
        ],
    )  # fmt: skip
    def test_expression_or_binding_refused(self, tmp_path, advice, params):
        members = (
            '    static final String PC = "@annotation(com.example.annot.PolicyCheck)";\n'
            '    @Pointcut("@annotation(check)")\n    void withParam(PolicyCheck check) {\n    }\n'
            '    @Pointcut("loop()")\n    void loop() {\n    }\n'
        )
        if "withParam" not in advice and "loop" not in advice:
            members = members.split("    @Pointcut")[0]
        assert not _links(tmp_path, _with_aspect(_aspect(advice, params, members=members)))

    @pytest.mark.parametrize(
        "extra,expected",
        [
            ({}, True),
            ({"aspect/Object.java": "package com.example.aspect;\n\npublic class Object {\n}\n"},
             False),
        ],
        ids=["java_lang_object", "same_package_object"],
    )  # fmt: skip
    def test_returning_object_must_be_java_lang_object(self, tmp_path, extra, expected):
        advice = f'@AfterReturning(pointcut = "@annotation({FQN})", returning = "r")'
        assert _links(tmp_path, _with_aspect(_aspect(advice, "Object r"), **extra)) is expected

    @pytest.mark.parametrize(
        "sibling", ["execution(* com.example..*(..))", "@annotation(PolicyCheck)"]
    )
    def test_a_refused_sibling_pointcut_refuses_every_named_reference(self, tmp_path, sibling):
        sibling = f'    @Pointcut("{sibling}")\n    void services() {{\n    }}\n'
        checked = f'    @Pointcut("@annotation({FQN})")\n    void checked() {{\n    }}\n'
        refused = _aspect('@Before("checked()")', members=checked + sibling)
        assert not _links(tmp_path / "refused", _with_aspect(refused))
        # An advice that references no named pointcut is unaffected.
        direct = _aspect(f'@Before("@annotation({FQN})")', members=checked + sibling)
        assert _links(tmp_path / "direct", _with_aspect(direct))

    @pytest.mark.parametrize(
        "header,extra",
        [
            ('@Aspect("perthis(this(com.example.pay.TransferService))")\npublic class PolicyAspect', {}),
            ("@Aspect\npublic abstract class PolicyAspect", {}),
            ("@Aspect\npublic class PolicyAspect extends com.acme.AuditSupport", {}),
            ("@Aspect\n@org.aspectj.lang.annotation.DeclarePrecedence(\"*\")\npublic class PolicyAspect", {}),
            ("@Aspect #\npublic class PolicyAspect", {}),
            ("@Aspect\npublic class PolicyAspect",
             {"aspect/SubAspect.java": "package com.example.aspect;\n\n"
              "public class SubAspect extends PolicyAspect {\n}\n"}),
            ("@Aspect\npublic interface PolicyAspect", {}),
            ("@com.example.aspect.Aspect\npublic class PolicyAspect",
             {"aspect/Aspect.java": "package com.example.aspect;\n\npublic @interface Aspect {\n}\n"}),
            # The repository declares the framework's name itself.
            ("@Aspect\npublic class PolicyAspect",
             {"lib/Before.java": "package org.aspectj.lang.annotation;\n\npublic @interface Before {\n"
              "    String value();\n}\n"}),
        ],
        ids=["per_clause", "abstract", "supertype", "declare_precedence", "header_error",
             "extended", "interface", "not_aspectj", "framework_name_declared"],
    )  # fmt: skip
    def test_aspect_refused(self, tmp_path, header, extra):
        aspect = _aspect(f'@Before("@annotation({FQN})")', header=header)
        assert not _links(tmp_path, _with_aspect(aspect, **extra))

    def test_advice_header_that_does_not_parse_is_refused(self, tmp_path):
        aspect = _aspect(f'@Before("@annotation({FQN})")').replace(
            "public void advise() {", "public void advise() throws {"
        )
        assert not _links(tmp_path, _with_aspect(aspect))


# ── the Spring adapter: methods left out on the annotated side ───────────


class TestTargetSide:
    @pytest.mark.parametrize(
        "old,new,extra",
        [
            ("public String transfer", "private String transfer", {}),
            ("public String transfer", "public static String transfer", {}),
            ("public String transfer", "public final String transfer", {}),
            ("public class TransferService", "public final class TransferService", {}),
            ("public class TransferService", "@org.aspectj.lang.annotation.Aspect\npublic class TransferService", {}),
            ("public class TransferService", "public class TransferService implements org.aopalliance.intercept.MethodInterceptor", {}),
            ("public class TransferService", "public class TransferService implements BeanPostProcessor", {}),
            ("public class TransferService", "public class TransferService extends BaseAdvisor",
             {"pay/BaseAdvisor.java": "package com.example.pay;\n\npublic abstract class BaseAdvisor "
              "extends org.springframework.aop.support.AbstractPointcutAdvisor {\n}\n"}),
            ("public class TransferService", "public class TransferService extends OtherAspect",
             {"pay/OtherAspect.java": "package com.example.pay;\n\n@org.aspectj.lang.annotation.Aspect\n"
              "public class OtherAspect {\n}\n"}),
            ("public class TransferService", "@AuditAspect\npublic class TransferService",
             {"pay/AuditAspect.java": "package com.example.pay;\n\n@org.aspectj.lang.annotation.Aspect\n"
              "public @interface AuditAspect {\n}\n"}),
            ("public String transfer(String req) {", "public String transfer(String req) throws {", {}),
        ],
        ids=["private", "static", "final_method", "final_class", "aspect_class", "interceptor",
             "post_processor", "infrastructure_base", "aspect_subclass", "aspect_stereotype",
             "header_error"],
    )  # fmt: skip
    def test_method_left_out_gets_no_edge(self, tmp_path, old, new, extra):
        service = TRANSFER_SERVICE.replace(old, new).replace(
            "import com.example.annot.PolicyCheck;",
            "import com.example.annot.PolicyCheck;\nimport com.example.aspect.PolicyAspect;",
        )
        assert new in service
        files = {**BASE_FILES, **extra, "pay/TransferService.java": service}
        assert not _links(tmp_path, files)

    @pytest.mark.parametrize(
        "source",
        [
            "package com.example.pay;\n\nimport com.example.annot.PolicyCheck;\n\npublic enum Kind {\n"
            "    A;\n\n    @PolicyCheck\n    public String transfer(String r) {\n        return r;\n    }\n}\n",
            "package com.example.pay;\n\nimport com.example.annot.PolicyCheck;\n\npublic interface Api {\n"
            "    @PolicyCheck\n    default String transfer(String r) {\n        return r;\n    }\n}\n",
            "package com.example.pay;\n\nimport com.example.annot.PolicyCheck;\n\npublic abstract class "
            "Base {\n    @PolicyCheck\n    public abstract String transfer(String r);\n}\n",
        ],
        ids=["enum", "interface", "abstract_method"],
    )
    def test_enums_interfaces_and_abstract_methods_get_no_edge(self, tmp_path, source):
        files = {**BASE_FILES, "pay/Other.java": source}
        kb, analyzer = _analyze(tmp_path, files)
        assert not [c for c in _annotation_edges(analyzer) if "Other_" in c]


# ── the Spring adapter: names ────────────────────────────────────────────


def _usage(imports: str, annotation: str = "@PolicyCheck", package: str = "com.example.pay") -> str:
    return (
        f"package {package};\n\n{imports}\n\npublic class TransferService {{\n"
        f"    {annotation}\n    public String transfer(String req) {{\n        return req;\n    }}\n}}\n"
    )


class TestNameResolution:
    @pytest.mark.parametrize(
        "service,extra,expected",
        [
            (_usage("import com.example.annot.PolicyCheck;"), {}, True),
            (_usage("", f"@{FQN}"), {}, True),
            (_usage("import com.example.annot.*;"), {}, True),
            (_usage("", package="com.example.annot"), {}, True),
            # An explicit import of a library annotation binds the name elsewhere.
            (_usage("import org.lib.PolicyCheck;"), {}, False),
            # Nothing visibly imports the name.
            (_usage("import org.lib.*;"), {}, False),
            # A static import or another nested type of that name may shadow it.
            (_usage("import com.example.annot.PolicyCheck;\nimport static org.lib.Holder.PolicyCheck;"), {}, False),
            (_usage("import com.example.annot.PolicyCheck;"),
             {"pay/Holder.java": "package com.example.pay;\n\npublic class Holder {\n"
              "    public @interface PolicyCheck {\n    }\n}\n"}, False),
        ],
        ids=["import", "qualified", "on_demand", "same_package", "library_import", "unresolved", "static_import", "nested_type"],
    )  # fmt: skip
    def test_usage_name(self, tmp_path, service, extra, expected):
        files = {**BASE_FILES, **extra, "pay/TransferService.java": service}
        assert _links(tmp_path, files) is expected

    def test_name_two_on_demand_imports_provide_is_ambiguous(self, tmp_path):
        other = "com.example.other.PolicyCheck"
        aspect = _aspect(f'@Before("@annotation({FQN}) || @annotation({other})")')
        service = _usage("import com.example.annot.*;\nimport com.example.other.*;")
        files = _with_aspect(aspect, **{
            "other/PolicyCheck.java": POLICY_CHECK.replace("annot;", "other;"),
            "pay/TransferService.java": service,
        })  # fmt: skip
        assert not _links(tmp_path, files)

    def test_library_annotation_of_the_same_simple_name_is_not_linked(self, tmp_path):
        """The Hibernate ``@Audited`` trap: an imported library annotation
        never matches an in-repo annotation of the same simple name."""
        audited = POLICY_CHECK.replace("PolicyCheck", "Audited").replace("annot;", "pay;")
        aspect = _aspect('@Before("@annotation(com.example.pay.Audited)")')
        service = _usage("import org.hibernate.envers.Audited;", "@Audited")
        files = _with_aspect(
            aspect, **{"pay/Audited.java": audited, "pay/TransferService.java": service}
        )
        assert not _links(tmp_path / "library", files)
        files["pay/TransferService.java"] = _usage("", "@Audited")  # same package: linked
        assert _links(tmp_path / "same_package", files)

    def test_imported_nested_annotation_links(self, tmp_path):
        holder = (
            "package com.example.annot;\n\npublic class Holder {\n"
            + "\n".join("    " + line for line in POLICY_CHECK.split("\n")[5:9])
            .replace("PolicyCheck", "Marker")
            .replace(
                "@Retention(RetentionPolicy.RUNTIME)",
                "@java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.RUNTIME)",
            )
            + "\n}\n"
        )
        aspect = _aspect('@Before("@annotation(com.example.annot.Holder.Marker)")')
        service = _usage("import com.example.annot.Holder.Marker;", "@Marker")
        files = _with_aspect(
            aspect, **{"annot/Holder.java": holder, "pay/TransferService.java": service}
        )
        assert _links(tmp_path, files)

    @pytest.mark.parametrize(
        "imports,retention,expected",
        [
            ("import java.lang.annotation.*;", "@Retention(RetentionPolicy.RUNTIME)", True),
            ("", "@java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.RUNTIME)", True),
            ("import java.lang.annotation.Retention;\nimport static java.lang.annotation.RetentionPolicy.RUNTIME;",
             "@Retention(RUNTIME)", True),
            ("import java.lang.annotation.*;", "@Retention(value = RetentionPolicy.RUNTIME)", True),
            ("import java.lang.annotation.*;", "@Retention(RetentionPolicy.CLASS)", False),
            ("import java.lang.annotation.*;", "", False),
            ("import java.lang.annotation.Retention;", "@Retention(RUNTIME)", False),
            ("import org.lib.Retention;\nimport java.lang.annotation.RetentionPolicy;",
             "@Retention(RetentionPolicy.RUNTIME)", False),
        ],
    )  # fmt: skip
    def test_only_runtime_retention_links(self, tmp_path, imports, retention, expected):
        annotation = f"package com.example.annot;\n\n{imports}\n\n{retention}\npublic @interface PolicyCheck {{\n}}\n"
        files = {**BASE_FILES, "annot/PolicyCheck.java": annotation}
        assert _links(tmp_path, files) is expected


# ── the core contract ────────────────────────────────────────────────────


class TestCoreContract:
    def test_adapter_triples_are_deduplicated_sorted_and_contained(self, monkeypatch, caplog):
        def good(components, get_tree):
            return [("m", "h2", 9), ("m", "h1", 4), ("m", "h1", 3), ("m", "m", 1), ("a", "h1", 2)]

        def other(components, get_tree):
            return [("m", "h1", 7)]

        def broken(components, get_tree):
            raise RuntimeError("boom")

        adapters = [
            annotation_edges.Adapter("java", good),
            annotation_edges.Adapter("java", other, kind="test-kind"),
            annotation_edges.Adapter("python", broken),
        ]
        monkeypatch.setattr(annotation_edges, "_adapters", lambda: adapters)
        comps = [MagicMock(language="java"), MagicMock(language="python")]
        edges = annotation_edges.resolve_annotation_edges(comps, lambda c: None)
        # Each edge carries its adapter's kind; the default is EDGE_KIND_ANNOTATION.
        assert edges == {
            "a": [("h1", 2, EDGE_KIND_ANNOTATION)],
            "m": [
                ("h1", 3, EDGE_KIND_ANNOTATION),
                ("h1", 7, "test-kind"),
                ("h2", 9, EDGE_KIND_ANNOTATION),
            ],
        }
        assert "boom" in caplog.text

    def test_flow_analyzer_appends_edges_only_between_callables(self, tmp_path, monkeypatch):
        kb, _ = _analyze(tmp_path, BASE_FILES)
        transfer, post = _cid(kb, "TransferService.transfer"), _cid(kb, "TransferService.post")
        ledger = _cid(kb, ".LedgerService")
        monkeypatch.setattr(
            annotation_edges,
            "_adapters",
            lambda: [
                annotation_edges.Adapter(
                    "java", lambda comps, tree: [(transfer, post, 5), (transfer, ledger, 5)]
                )
            ],
        )
        _, analyzer = _analyze(tmp_path, BASE_FILES)
        assert _annotation_edges(analyzer) == {transfer: [post]}


# ── how the edges surface ────────────────────────────────────────────────

DUAL_ROLE_RUNNER = """package com.example.pay;

import com.example.aspect.PolicyAspect;

public class Runner {
    private TransferService svc;
    private PolicyAspect policyAspect;

    public String run(String req) {
        svc.transfer(req);
        return follow(req);
    }

    String follow(String req) {
        policyAspect.apply(null, null);
        return req;
    }
}
"""

DIRECT_FIRST_SERVICE = TRANSFER_SERVICE.replace(
    "import com.example.annot.PolicyCheck;",
    "import com.example.annot.PolicyCheck;\nimport com.example.aspect.PolicyAspect;",
).replace(
    "    String post(String req) {\n",
    "    private PolicyAspect policyAspect;\n\n    String post(String req) {\n"
    "        policyAspect.apply(null, null);\n",
)


def _find(node, target):
    if node["id"] == target:
        return node
    for child in node["calls"]:
        found = _find(child, target)
        if found is not None:
            return found
    return None


class TestToolVisibility:
    def test_show_dependencies_splits_advised_by_from_direct(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        transfer, advice = _cid(kb, "TransferService.transfer"), _cid(kb, "PolicyAspect.apply")
        deps = analyzer.analyze_dependencies(transfer)
        assert deps.advised_by == [advice]
        assert deps.direct_dependencies == [_cid(kb, "TransferService.post")]
        reverse = analyzer.analyze_dependencies(advice)
        assert reverse.advises == [transfer]
        assert transfer not in reverse.dependents
        text = _format_dependencies(deps.to_dict())
        assert "Advised by (implicit annotation handlers, not direct calls):" in text
        assert "Advises (methods this handler implicitly wraps):" in _format_dependencies(
            reverse.to_dict()
        )

    def test_trace_flow_marks_annotation_steps(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        advice = _cid(kb, "PolicyAspect.apply")
        flow = analyzer.trace_execution_flow(_cid(kb, "TransferService.transfer"), 5)
        step_types = {step.component: step.call_type for step in flow.flow_steps}
        assert step_types[advice] == CallType.ANNOTATION
        assert step_types[_cid(kb, "TransferService.post")] == CallType.DIRECT
        assert f"--(advised by, implicit)--> {advice}" in _format_flow(flow.to_dict())

    def test_call_graph_with_source_labels_the_handler(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        graph = analyzer.build_call_graph_with_source(_cid(kb, "TransferService.transfer"), 5)
        handler = next(n for n in graph["calls"] if n.get("edge_kind") == EDGE_KIND_ANNOTATION)
        assert handler["id"] == _cid(kb, "PolicyAspect.apply")
        assert "also_called_directly" not in handler
        human: list[str] = []
        _format_call_graph(graph, human, raw=False, seen=set())
        assert any("Relation: advised by (implicit annotation handler" in ln for ln in human)
        raw: list[str] = []
        _format_call_graph(graph, raw, raw=True, seen=set())
        assert any("[advised by: implicit annotation handler" in ln for ln in raw)

    def test_handler_rendered_under_a_direct_call_gets_a_dual_role_note(self, tmp_path):
        files = {**BASE_FILES, "pay/TransferService.java": DIRECT_FIRST_SERVICE}
        kb, analyzer = _analyze(tmp_path, files)
        advice = _cid(kb, "PolicyAspect.apply")
        graph = analyzer.build_call_graph_with_source(_cid(kb, "TransferService.transfer"), 5)
        handler = _find(graph, advice)
        assert "edge_kind" not in handler and handler["also_annotation_handler"] is True
        assert "also_annotation_handler" not in graph  # the root is never marked
        raw: list[str] = []
        _format_call_graph(graph, raw, raw=True, seen=set())
        header = next(ln for ln in raw if ln.strip().startswith(advice))
        assert "[also an implicit annotation handler for annotated methods in this graph]" in header
        assert "not a direct call" not in header

    def test_handler_rendered_under_an_annotation_edge_notes_direct_calls(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, {**BASE_FILES, "pay/Runner.java": DUAL_ROLE_RUNNER})
        advice = _cid(kb, "PolicyAspect.apply")
        graph = analyzer.build_call_graph_with_source(_cid(kb, "Runner.run"), 5)
        handler = _find(graph, advice)
        assert handler["edge_kind"] == EDGE_KIND_ANNOTATION
        assert handler["also_called_directly"] is True
        human: list[str] = []
        _format_call_graph(graph, human, raw=False, seen=set())
        assert "Note: also called directly elsewhere in this graph" in "\n".join(human)


class TestUnlabeledDependencyOutputs:
    """Implicit edges never appear in an unlabeled dependency output."""

    def test_dependency_depth_ignores_annotation_edges(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        settle = analyzer.analyze_dependencies(_cid(kb, "LedgerService.settle"))
        assert settle.advised_by == [_cid(kb, "PolicyAspect.applyToType")]
        assert settle.dependency_depth == 0
        assert (
            analyzer.analyze_dependencies(_cid(kb, "TransferService.transfer")).dependency_depth
            == 1
        )

    def test_plainly_reachable_handler_stays_a_transitive_dependency(self, tmp_path):
        files = {**BASE_FILES, "pay/TransferService.java": DIRECT_FIRST_SERVICE}
        kb, analyzer = _analyze(tmp_path, files)
        advice = _cid(kb, "PolicyAspect.apply")
        deps = analyzer.analyze_dependencies(_cid(kb, "TransferService.transfer"))
        assert advice in deps.transitive_dependencies
        assert advice not in deps.direct_dependencies
        assert advice in deps.advised_by

    def test_find_dependency_path_does_not_cross_annotation_edges(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        transfer, post = _cid(kb, "TransferService.transfer"), _cid(kb, "TransferService.post")
        assert analyzer.find_dependency_path(transfer, _cid(kb, "PolicyAspect.apply")) is None
        assert analyzer.find_dependency_path(transfer, post) == [transfer, post]

    def test_cycles_closed_only_by_annotation_edges_are_not_reported(self, tmp_path):
        aspect = (
            POLICY_ASPECT.replace(
                "import com.example.annot.PolicyCheck;",
                "import com.example.annot.PolicyCheck;\nimport com.example.pay.TransferService;",
            )
            .replace("    @Around", "    private TransferService transferService;\n\n    @Around")
            .replace(
                "return pjp.proceed();",
                'transferService.transfer("retry");\n        return pjp.proceed();',
            )
        )
        kb, analyzer = _analyze(tmp_path, _with_aspect(aspect))
        transfer = _cid(kb, "TransferService.transfer")
        flow = analyzer.trace_execution_flow(transfer, 10)
        assert flow.circular_calls == []
        assert analyzer.analyze_dependencies(transfer).circular_dependencies == []

    def test_plain_call_cycles_are_still_reported(self, tmp_path):
        service = TRANSFER_SERVICE.replace(
            "        return req;\n    }\n}", "        return transfer(req);\n    }\n}"
        )
        kb, analyzer = _analyze(tmp_path, {**BASE_FILES, "pay/TransferService.java": service})
        transfer = _cid(kb, "TransferService.transfer")
        assert transfer in analyzer.trace_execution_flow(transfer, 10).circular_calls


class TestSummarizationInput:
    def test_handler_source_is_in_the_prompt_and_replaced_once_described(self, tmp_path):
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        advice = _cid(kb, "PolicyAspect.apply")
        graph = analyzer.build_call_graph_with_source(_cid(kb, "TransferService.transfer"), 5)
        output: list[str] = []
        TopDownSummarizer._format_call_graph(graph, output, seen=set())
        text = "\n".join(output)
        assert f"{advice} (method) [annotation handler" in text
        assert "return pjp.proceed();" in text
        summarizer = TopDownSummarizer.__new__(TopDownSummarizer)
        summarizer._description_cache = {advice: "Enforces the policy."}
        rewritten = summarizer._replace_source_with_descriptions(text)
        assert "Enforces the policy." in rewritten
        assert "pjp.proceed()" not in rewritten

    def test_dual_role_notes_keep_the_description_rewrite_working(self, tmp_path):
        for subdir, files, root, note in (
            ("direct", {**BASE_FILES, "pay/TransferService.java": DIRECT_FIRST_SERVICE},
             "TransferService.transfer",
             "[also an implicit annotation handler for annotated methods in this graph]"),
            ("annotation", {**BASE_FILES, "pay/Runner.java": DUAL_ROLE_RUNNER}, "Runner.run",
             "[annotation handler: runs implicitly for the annotated caller above] "
             "[also called directly elsewhere in this graph]"),
        ):  # fmt: skip
            kb, analyzer = _analyze(tmp_path / subdir, files)
            advice = _cid(kb, "PolicyAspect.apply")
            graph = analyzer.build_call_graph_with_source(_cid(kb, root), 5)
            output: list[str] = []
            TopDownSummarizer._format_call_graph(graph, output, seen=set())
            text = "\n".join(output)
            assert f"{advice} (method) {note}" in text
            summarizer = TopDownSummarizer.__new__(TopDownSummarizer)
            summarizer._description_cache = {advice: "Enforces the policy."}
            rewritten = summarizer._replace_source_with_descriptions(text)
            assert "Enforces the policy." in rewritten and "pjp.proceed()" not in rewritten

    def test_annotation_edges_count_toward_call_depth(self, tmp_path):
        """An advised method is summarized after (above) its handler."""
        kb, analyzer = _analyze(tmp_path, BASE_FILES)
        depths = analyzer.get_call_depths()
        assert (
            depths[_cid(kb, "LedgerService.settle")]
            == depths[_cid(kb, "PolicyAspect.applyToType")] + 1
        )


# ── refresh staleness ────────────────────────────────────────────────────


def _refresh_comp(cid: str, file: str, *, content_hash: str, desc: str = "",
                  comp_type: ComponentType = ComponentType.METHOD) -> Component:  # fmt: skip
    return Component(
        id=cid,
        type=comp_type,
        file_path=Path(file),
        line_number=1,
        end_line_number=5,
        language="java",
        space_id="space",
        business_behavior=BusinessBehavior(description=desc),
        content_hash=content_hash,
    )


def _refresh_orchestrator(prior_kb: KnowledgeBase) -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = prior_kb
    return orch


class TestRefreshStaleness:
    def test_changing_only_the_advice_marks_the_annotated_method_stale(self):
        prior_kb = KnowledgeBase(
            source_directory=Path("/repo"),
            space_id="space",
            components=[
                _refresh_comp("space::T.transfer", "T.java", content_hash="ht", desc="moves money"),
                _refresh_comp("space::A.apply", "A.java", content_hash="ha", desc="checks policy"),
            ],
            call_graph={"space::T.transfer": [["space::A.apply", 7, EDGE_KIND_ANNOTATION]]},
        )
        orch = _refresh_orchestrator(prior_kb)
        orch._file_changes = [FileChange("A.java", ChangeType.MODIFIED)]
        orch._parsed_components = [_refresh_comp("space::A.apply", "A.java", content_hash="ha2")]
        orch._step_merge()
        assert "space::T.transfer" in orch._refresh_plan.stale_caller_ids

    @pytest.mark.parametrize("added", [True, False])
    def test_annotation_edge_appearing_or_disappearing_marks_the_method_stale(self, added):
        edge = {"space::L.settle": [["space::A.applyToType", 1, EDGE_KIND_ANNOTATION]]}
        prior_kb = KnowledgeBase(
            source_directory=Path("/repo"),
            space_id="space",
            components=[
                _refresh_comp("space::L.settle", "L.java", content_hash="hs", desc="settles"),
                _refresh_comp("space::A.applyToType", "A.java", content_hash="ha", desc="advice"),
            ],
            call_graph={} if added else edge,
        )
        orch = _refresh_orchestrator(prior_kb)
        orch._file_changes = [FileChange("L.java", ChangeType.MODIFIED)]
        orch._parsed_components = [_refresh_comp("space::L.settle", "L.java", content_hash="hs")]
        orch._step_merge()
        assert "space::L.settle" not in orch._refresh_plan.stale_caller_ids  # its hash is unchanged
        orch._widen_stale_callers(prior_kb.call_graph, edge if added else {"space::L.settle": []})
        assert "space::L.settle" in orch._refresh_plan.stale_caller_ids

    def test_annotation_edge_changes_ignore_plain_call_churn(self):
        changes = RefreshOrchestrator._annotation_edge_changes(
            {"space::a": [["space::b", 3]], "space::c": [["space::h", 1, EDGE_KIND_ANNOTATION]]},
            {"space::a": [["space::d", 9]], "space::c": [("space::h", 2, EDGE_KIND_ANNOTATION)]},
        )
        assert changes == frozenset()

    def test_end_to_end_refresh_adding_a_class_annotation_resummarizes_its_methods(self, tmp_path):
        old_files = {
            **BASE_FILES,
            "pay/LedgerService.java": LEDGER_SERVICE.replace('@PolicyCheck("P-9")\n', ""),
        }
        old_root, clone = tmp_path / "old", tmp_path / "clone"
        for root, sources in ((old_root, old_files), (clone, BASE_FILES)):
            for rel, body in sources.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(body)
        prior_kb = CodeAnalyzer(ComponentCatalog(None)).analyze_directory(old_root, "space")
        backfill_component_hashes(old_root, prior_kb.components)
        catalog = ComponentCatalog(None)
        for comp in prior_kb.components:
            catalog.add_component(comp)
        flow = FlowAnalyzer(catalog)
        flow.build_call_graph(prior_kb.components)
        prior_kb.call_graph = flow._call_graph
        for comp in prior_kb.components:  # persisted KBs hold repo-relative paths
            comp.file_path = Path(comp.file_path).resolve().relative_to(old_root.resolve())

        orch = _refresh_orchestrator(prior_kb)
        orch._clone_dir = clone
        orch._new_commit_hash = "new"
        orch._file_changes = [FileChange("pay/LedgerService.java", ChangeType.MODIFIED)]
        orch._step_parse_changed()
        orch._step_merge()
        settle = _cid(prior_kb, "LedgerService.settle")
        assert settle not in orch._refresh_plan.components_to_summarize()

        orch._step_rebuild_call_graph()
        assert _cid(prior_kb, "PolicyAspect.applyToType") in [
            call_edge_target(e) for e in prior_kb.call_graph.get(settle, []) if call_edge_kind(e)
        ]
        assert settle in orch._refresh_plan.components_to_summarize()
        assert (
            _cid(prior_kb, "LedgerService.hidden")
            not in orch._refresh_plan.components_to_summarize()
        )


# ── backward compatibility ───────────────────────────────────────────────


class TestBackwardCompatibility:
    def test_legacy_call_graph_without_edge_kinds_still_loads(self):
        kb = KnowledgeBase.from_dict(
            {
                "source_directory": "/repo",
                "components": [
                    _refresh_comp("space::S.a", "S.java", content_hash="h1").to_dict(),
                    _refresh_comp("space::S.b", "S.java", content_hash="h2").to_dict(),
                ],
                "call_graph": {"space::S.a": [["space::S.b", 4]]},
                "space_id": "space",
            }
        )
        deps = (
            AnalysisOrchestrator(MagicMock())
            ._initialize_context(kb)
            .flow_analyzer.analyze_dependencies("space::S.a")
        )
        assert deps.direct_dependencies == ["space::S.b"]
        assert deps.advised_by == [] and deps.advises == []
        assert "advised_by" not in deps.to_dict() and "advises" not in deps.to_dict()

    def test_mixed_edge_shapes_reconstruct_and_round_trip(self):
        call_graph = {"space::S.a": [["space::S.b", 4], ["space::H.h", 2, EDGE_KIND_ANNOTATION]]}
        reconstructed = AnalysisOrchestrator(MagicMock())._reconstruct_call_graph(call_graph)
        assert reconstructed["space::S.a"] == [
            ("space::S.b", 4),
            ("space::H.h", 2, EDGE_KIND_ANNOTATION),
        ]
        kb = KnowledgeBase(source_directory=Path("/r"), call_graph=call_graph)
        assert KnowledgeBase.from_dict(kb.to_dict()).call_graph == call_graph

    def test_new_fields_default_when_absent(self):
        deps = DependencyAnalysis.from_dict({"component": "space::S.a", "direct_dependencies": []})
        assert deps.advised_by == [] and deps.advises == []
        step = FlowStep(sequence_number=1, component="space::H.h", caller="space::S.a",
                        call_type=CallType.ANNOTATION, file_location="space::S.a:7", line_number=7)  # fmt: skip
        assert FlowStep.from_dict(step.to_dict()).call_type == CallType.ANNOTATION
        data = _refresh_comp("space::S.a", "S.java", content_hash="h1").to_dict()
        del data["attributes"]["decorators"]
        assert Component.from_dict(data).attributes.decorators == []
