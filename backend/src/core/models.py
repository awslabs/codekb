"""
Core data models for the Code Knowledge Base system.

This module defines the fundamental data structures used throughout
the application, following the domain model from the design phase.
"""

import hashlib
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# Component ID scheme
#
# A component id is `<space_id>::<local_id>`, where `local_id` is a dotted
# path of segments (e.g. `service.PaymentService.handle`). The separators and
# every operation that builds or reads an id live HERE so parsers, the
# analyzer, the MCP layer, and the model itself all share one definition and
# never hand-roll the format.
#
# The *local_id* top-level prefix is scheme-dependent (see IdScheme). The
# scheme is a per-space property, recorded on the persisted KnowledgeBase as
# `id_scheme` so that a space keeps its original scheme across refreshes and
# only adopts a new scheme on a full reindex.
# ---------------------------------------------------------------------------

SPACE_SEP = "::"  # separates space_id from the local component id
NAME_SEP = "."  # separates dotted segments within a local id
# Separates an overload discriminator from the bare name in an id's trailing
# segment (`Repo.Save#a1b2`). It cannot occur in an identifier in any supported
# language, so the bare name is always recoverable from the id alone.
DISAMBIG_SEP = "#"


class IdScheme(Enum):
    """How a parser derives the top-level prefix of a local component id.

    V1_STEM: legacy scheme — prefix is the source file *stem* (basename minus
        extension). Collision-prone: same-named symbols in same-named files in
        different directories/packages produce identical ids.
    V2_PATH: prefix is a stable hash of the file's repo-relative path, so the
        same symbol name in different files never collides.
    """

    V1_STEM = "v1"
    V2_PATH = "v2"


# Scheme applied to newly indexed spaces. Existing spaces keep whatever scheme
# is recorded in their persisted knowledge base (see resolve_id_scheme).
DEFAULT_SCHEME = IdScheme.V2_PATH


def resolve_id_scheme(marker: Optional[str]) -> IdScheme:
    """Resolve a persisted `id_scheme` marker to an IdScheme.

    Anything missing/empty/legacy (`""`, ``None``, ``"v1"``, ``"1.0"``) resolves
    to V1_STEM so already-indexed spaces keep their original id format on
    refresh. Only an explicit v2 marker selects the new scheme.
    """
    if not marker:
        return IdScheme.V1_STEM
    normalized = str(marker).strip().lower()
    if normalized in ("v2", "2.0"):
        return IdScheme.V2_PATH
    return IdScheme.V1_STEM


def path_prefix(file_path: Path, root: Optional[Path]) -> str:
    """Return the V2 top-level prefix: a stable hash of the repo-relative path.

    Falls back to the file stem when no root is known (root should always be
    provided for V2 indexing; the fallback only guards against misconfiguration
    so id construction never raises).
    """
    if root is not None:
        try:
            relative = file_path.resolve().relative_to(Path(root).resolve())
            rel_posix = relative.as_posix()
        except (ValueError, OSError):
            rel_posix = file_path.name
    else:
        rel_posix = file_path.name
    digest = hashlib.sha1(rel_posix.encode("utf-8")).hexdigest()[:12]
    return f"{file_path.stem}_{digest}"


def qualify(space_id: str, local_id: str) -> str:
    """Prefix a local id with `space_id::`, idempotently."""
    prefix = f"{space_id}{SPACE_SEP}"
    if local_id.startswith(prefix):
        return local_id
    return f"{prefix}{local_id}"


def split_space(qualified_id: str) -> Tuple[str, str]:
    """Split a fully-qualified id into (space_id, qualified_id).

    Raises:
        ValueError: if the input is not space-qualified with `::`.
    """
    if SPACE_SEP not in qualified_id:
        raise ValueError(f"'{qualified_id}' is not space-qualified with '{SPACE_SEP}'")
    space_id, _ = qualified_id.split(SPACE_SEP, 1)
    if not space_id:
        raise ValueError(f"Missing space_id in '{qualified_id}'")
    return space_id, qualified_id


def simple_name(qualified_or_local_id: str) -> str:
    """Return the last dotted segment of an id (its bare symbol name)."""
    return qualified_or_local_id.split(NAME_SEP)[-1]


def _terraform_address(local_id: str) -> str:
    """Return the Terraform reference address held at the end of a local id.

    Works for V1 ids (`aws_s3_bucket.logs`) and V2 ids
    (`main_<hash>.aws_s3_bucket.logs`). Taken from the END of the id because a
    V2 file prefix can itself contain dots (`network.prod_<hash>`).
    `data.TYPE.NAME` is three segments; every other address (TYPE.NAME, var.X,
    local.X, module.X, output.X, provider.X) is two.
    """
    parts = local_id.split(NAME_SEP)
    if len(parts) >= 3 and parts[-3] == "data":
        return NAME_SEP.join(parts[-3:])
    return NAME_SEP.join(parts[-2:])


class ComponentType(Enum):
    """Enumeration of supported component types."""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    MODULE = "module"
    INTERFACE = "interface"
    VARIABLE = "variable"
    CONSTANT = "constant"
    ENUM = "enum"
    CONSTRUCTOR = "constructor"


class Visibility(Enum):
    """Enumeration of component visibility levels."""

    PUBLIC = "public"
    PRIVATE = "private"
    PROTECTED = "protected"
    INTERNAL = "internal"
    PACKAGE = "package"


class CallType(Enum):
    """Enumeration of call relationship types."""

    DIRECT = "direct"
    CONDITIONAL = "conditional"
    LOOP = "loop"
    RECURSIVE = "recursive"


class CouplingStrength(Enum):
    """Enumeration of dependency coupling strength levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Parameter:
    """
    Represents a function or method parameter.

    Attributes:
        name: Parameter name
        type_hint: Type annotation if available
        default_value: Default value if parameter is optional
        required: Whether the parameter is required
        style: How the parameter is passed, when the language distinguishes it:
            "positional-required", "positional-optional" or "named". None for
            languages that do not record it, and then omitted from to_dict()
    """

    name: str
    type_hint: Optional[str] = None
    default_value: Optional[str] = None
    required: bool = True
    style: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert parameter to dictionary representation."""
        data: Dict[str, Any] = {
            "name": self.name,
            "type_hint": self.type_hint,
            "default_value": self.default_value,
            "required": self.required,
        }
        # Additive and opt-in: a parameter from a language that does not record
        # a passing style serializes exactly as it did before this field existed.
        if self.style is not None:
            data["style"] = self.style
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Parameter":
        """Create parameter from dictionary representation."""
        return cls(
            name=data["name"],
            type_hint=data.get("type_hint"),
            default_value=data.get("default_value"),
            required=data.get("required", True),
            style=data.get("style"),
        )


@dataclass
class ComponentAttributes:
    """
    Detailed attributes and properties of a code component.

    Attributes:
        signature: Function/method signature
        parameters: List of parameters for functions/methods
        return_type: Return type annotation if available
        visibility: Component visibility level
        documentation: Associated docstring or comments
        complexity_score: Cyclomatic complexity score
        decorators: List of decorators or annotations
        line_count: Number of lines in the component
        is_static: Whether the component is static (Java, etc.)
        is_abstract: Whether the component is abstract
        is_final: Whether the component is final/sealed
        accessor_kind: "get" or "set" when the identity carries that kind suffix,
            else None. Recorded because the suffix is indistinguishable from a
            member literally named `get`/`set`, which Dart permits
    """

    signature: Optional[str] = None
    parameters: List[Parameter] = field(default_factory=list)
    return_type: Optional[str] = None
    visibility: Visibility = Visibility.PUBLIC
    documentation: Optional[str] = None
    complexity_score: int = 1
    decorators: List[str] = field(default_factory=list)
    line_count: int = 0
    is_static: bool = False
    is_abstract: bool = False
    is_final: bool = False
    accessor_kind: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert attributes to dictionary representation."""
        data: Dict[str, Any] = {
            "signature": self.signature,
            "parameters": [p.to_dict() for p in self.parameters],
            "return_type": self.return_type,
            "visibility": self.visibility.value,
            "documentation": self.documentation,
            "complexity_score": self.complexity_score,
            "decorators": self.decorators,
            "line_count": self.line_count,
            "is_static": self.is_static,
            "is_abstract": self.is_abstract,
            "is_final": self.is_final,
        }
        # Additive and opt-in: a component from a language with no kind suffix
        # serializes exactly as it did before this field existed.
        if self.accessor_kind is not None:
            data["accessor_kind"] = self.accessor_kind
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComponentAttributes":
        """Create attributes from dictionary representation."""
        return cls(
            signature=data.get("signature"),
            parameters=[Parameter.from_dict(p) for p in data.get("parameters", [])],
            return_type=data.get("return_type"),
            visibility=Visibility(data.get("visibility", "public")),
            documentation=data.get("documentation"),
            complexity_score=data.get("complexity_score", 1),
            decorators=data.get("decorators", []),
            line_count=data.get("line_count", 0),
            is_static=data.get("is_static", False),
            is_abstract=data.get("is_abstract", False),
            is_final=data.get("is_final", False),
            accessor_kind=data.get("accessor_kind"),
        )


@dataclass
class BusinessBehavior:
    """
    AI-powered analysis of component business behavior.

    Attributes:
        description: Natural language description of component purpose
        domain_concepts: Business domain concepts identified
        design_patterns: Design patterns detected
        confidence_score: Confidence in the analysis (0.0 to 1.0)
        business_rules: Identified business rules or constraints
        business_relevance_score: Heuristic score (0-100) estimating how much
            business meaning this component carries in the full codebase
        business_relevance_tier: Suggested summarization tier derived from the score
        business_relevance_reasons: Human-readable signals that influenced the score
        business_relevance_signals: In-memory diagnostic scoring inputs. Not persisted
            (omitted from to_dict) — nothing reads them for a decision and the UI does
            not render them. Defaults to {} when loaded from disk.
        business_relevance_contributions: In-memory diagnostic score breakdown. Not
            persisted, for the same reason. Defaults to {} when loaded from disk.
    """

    description: str = ""
    domain_concepts: List[str] = field(default_factory=list)
    design_patterns: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    business_rules: List[str] = field(default_factory=list)
    business_relevance_score: float = 0.0
    business_relevance_tier: str = "low"
    business_relevance_reasons: List[str] = field(default_factory=list)
    business_relevance_signals: Dict[str, Any] = field(default_factory=dict)
    business_relevance_contributions: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert business behavior to dictionary representation."""
        return {
            "description": self.description,
            "domain_concepts": self.domain_concepts,
            "design_patterns": self.design_patterns,
            "confidence_score": self.confidence_score,
            "business_rules": self.business_rules,
            "business_relevance_score": self.business_relevance_score,
            "business_relevance_tier": self.business_relevance_tier,
            "business_relevance_reasons": self.business_relevance_reasons,
        }

    def is_empty(self) -> bool:
        """Return True when no business behavior data has been populated.

        Every persisted field is at its constructor default. This is the state
        of a component analyzed with the business-behavior feature disabled: no
        analyzer ran, no relevance scoring ran, and it was not summarized.
        ``Component.to_dict`` uses this to omit the whole block from the stored
        knowledge base so a disabled space writes nothing for it. A summarized
        component is NOT empty — its LLM summary is stored in ``description`` —
        so its block is always kept.
        """
        return (
            not self.description
            and not self.domain_concepts
            and not self.design_patterns
            and self.confidence_score == 0.0
            and not self.business_rules
            and self.business_relevance_score == 0.0
            and self.business_relevance_tier == "low"
            and not self.business_relevance_reasons
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BusinessBehavior":
        """Create business behavior from dictionary representation."""
        return cls(
            description=data.get("description", ""),
            domain_concepts=data.get("domain_concepts", []),
            design_patterns=data.get("design_patterns", []),
            confidence_score=data.get("confidence_score", 0.0),
            business_rules=data.get("business_rules", []),
            business_relevance_score=data.get("business_relevance_score", 0.0),
            business_relevance_tier=data.get("business_relevance_tier", "low"),
            business_relevance_reasons=data.get("business_relevance_reasons", []),
            business_relevance_signals=data.get("business_relevance_signals", {}),
            business_relevance_contributions=data.get("business_relevance_contributions", {}),
        )


@dataclass
class Component:
    """
    Represents a code component (function, class, module, etc.).

    Attributes:
        id: Unique component identifier (serves as both id and name)
        type: Component type (function, class, etc.)
        file_path: Source file path
        line_number: Starting line number
        end_line_number: Ending line number
        language: Programming language
        space_id: Space identifier for multi-space support
        attributes: Detailed component attributes
        business_behavior: AI-analyzed business behavior
        parent_component: Parent component ID if nested
        content_hash: Stable hash of the component's source lines, used by the
            space-refresh merge to detect which components in a changed file
            actually changed (method-level change detection). None for spaces
            analyzed before this field existed; backfilled on the next refresh.
    """

    id: str
    type: ComponentType
    file_path: Path
    line_number: int
    end_line_number: int
    language: str
    space_id: str = ""
    attributes: ComponentAttributes = field(default_factory=ComponentAttributes)
    business_behavior: BusinessBehavior = field(default_factory=BusinessBehavior)
    parent_component: Optional[str] = None
    content_hash: Optional[str] = None

    def __post_init__(self) -> None:
        """Post-initialization validation and setup."""
        if self.line_number > self.end_line_number:
            raise ValueError("line_number cannot be greater than end_line_number")
        if not self.id.strip():
            raise ValueError("Component id cannot be empty")

    @property
    def name(self) -> str:
        """Get the display name. Always the id; kept for API/serialization compatibility."""
        return self.id

    def get_simple_name(self) -> str:
        """Get the simple component name without parent context.

        Dart is the one language whose identity can end in a kind suffix, so a
        getter for `label` reads `file.Class.label.get` and the suffix has to be
        stepped over. Dart also allows `get` and `set` as ordinary method names,
        so the trailing segment alone cannot say which of the two this is.

        `accessor_kind` answers it outright. A component persisted before that
        field existed carries none, and then the id's shape against its parent
        decides: a member's id is its parent plus one segment, an accessor's is
        its parent plus two. A parentless accessor from such a record reports its
        kind until the next refresh re-parses it.

        The name is derived from the id alone. An overload discriminator on the
        trailing segment (`...Repo.Save#a1b2`) is stripped, so every overload
        reports the bare name and call resolution keys on it. A Terraform id
        reports its reference address (`aws_iam_role.ecs_task_role`), which is
        what Terraform references name.
        """
        if self.language == "terraform":
            return _terraform_address(self.id.split(SPACE_SEP, 1)[-1])

        segments = self.id.split(NAME_SEP)
        segments[-1] = segments[-1].split(DISAMBIG_SEP, 1)[0]
        if self.language != "dart" or len(segments) < 2:
            return segments[-1]

        if self.attributes.accessor_kind is not None:
            return segments[-2]

        if segments[-1] not in ("get", "set"):
            return segments[-1]

        parent = self.parent_component
        if parent and self.id.startswith(f"{parent}."):
            remainder = self.id[len(parent) + 1 :]
            # `label.get` is an accessor; a bare `get` is the member's own name.
            return segments[-2] if "." in remainder else segments[-1]
        return segments[-1]

    def get_full_name(self) -> str:
        """Get the fully qualified component name (same as id)."""
        return self.id

    def get_location_string(self) -> str:
        """
        Get a human-readable location string.

        Returns:
            Location string in format "file:line-endline"
        """
        return f"{self.file_path}:{self.line_number}-{self.end_line_number}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert component to dictionary representation.

        The ``business_behavior`` block is omitted entirely when it holds no
        data (see ``BusinessBehavior.is_empty``). Spaces analyzed with the
        business-behavior feature disabled therefore store nothing for it,
        while ``from_dict`` restores an empty ``BusinessBehavior`` when the key
        is absent, so readers are unaffected.
        """
        data: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,  # always equal to the id
            "type": self.type.value,
            "file_path": str(self.file_path),
            "line_number": self.line_number,
            "end_line_number": self.end_line_number,
            "language": self.language,
            "space_id": self.space_id,
            "attributes": self.attributes.to_dict(),
            "parent_component": self.parent_component,
            "content_hash": self.content_hash,
        }
        if not self.business_behavior.is_empty():
            data["business_behavior"] = self.business_behavior.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Component":
        """Create component from dictionary representation."""
        # Use id if available, fallback to name for backward compatibility.
        # Otherwise a persisted "name" is ignored: the name is always the id,
        # including for spaces that once stored a separate short name.
        component_id = data.get("id", data.get("name", ""))
        return cls(
            id=component_id,
            type=ComponentType(data["type"]),
            file_path=Path(data["file_path"]),
            line_number=data["line_number"],
            end_line_number=data["end_line_number"],
            language=data["language"],
            space_id=data.get("space_id", data.get("project_id", "")),
            attributes=ComponentAttributes.from_dict(data.get("attributes", {})),
            business_behavior=BusinessBehavior.from_dict(data.get("business_behavior", {})),
            parent_component=data.get("parent_component"),
            content_hash=data.get("content_hash"),
        )


@dataclass
class FlowStep:
    """
    Represents a single step in an execution flow.

    Attributes:
        sequence_number: Order in the execution flow
        component: Component being called
        caller: Component making the call
        call_type: Type of call relationship
        file_location: File and line where call occurs
        line_number: Line number where the call occurs
        conditional_info: Additional info for conditional calls
    """

    sequence_number: int
    component: str
    caller: str
    call_type: CallType
    file_location: str
    line_number: Optional[int] = None
    conditional_info: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert flow step to dictionary representation."""
        return {
            "sequence_number": self.sequence_number,
            "component": self.component,
            "caller": self.caller,
            "call_type": self.call_type.value,
            "file_location": self.file_location,
            "line_number": self.line_number,
            "conditional_info": self.conditional_info,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FlowStep":
        """Create flow step from dictionary representation."""
        return cls(
            sequence_number=data["sequence_number"],
            component=data["component"],
            caller=data["caller"],
            call_type=CallType(data["call_type"]),
            file_location=data["file_location"],
            line_number=data.get("line_number"),
            conditional_info=data.get("conditional_info"),
        )


@dataclass
class ExecutionFlow:
    """
    Represents a traced execution flow through components.

    Attributes:
        start_component: Starting component for the flow
        flow_steps: Ordered list of execution steps
        total_depth: Maximum depth reached in the trace
        circular_calls: Components involved in circular calls
        incomplete: Whether the trace was truncated due to depth limits
    """

    start_component: str
    flow_steps: List[FlowStep] = field(default_factory=list)
    total_depth: int = 0
    circular_calls: List[str] = field(default_factory=list)
    incomplete: bool = False

    def add_step(self, step: FlowStep) -> None:
        """Add a step to the execution flow."""
        self.flow_steps.append(step)
        self.total_depth = max(self.total_depth, step.sequence_number)

    def get_called_components(self) -> List[str]:
        """Get list of all components called in this flow."""
        return [step.component for step in self.flow_steps]

    def to_dict(self) -> Dict[str, Any]:
        """Convert execution flow to dictionary representation."""
        return {
            "start_component": self.start_component,
            "flow_steps": [step.to_dict() for step in self.flow_steps],
            "total_depth": self.total_depth,
            "circular_calls": self.circular_calls,
            "incomplete": self.incomplete,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionFlow":
        """Create execution flow from dictionary representation."""
        return cls(
            start_component=data["start_component"],
            flow_steps=[FlowStep.from_dict(step) for step in data.get("flow_steps", [])],
            total_depth=data.get("total_depth", 0),
            circular_calls=data.get("circular_calls", []),
            incomplete=data.get("incomplete", False),
        )


@dataclass
class DependencyAnalysis:
    """
    Analysis of component dependencies and relationships.

    Attributes:
        component: Component being analyzed
        direct_dependencies: Components directly depended upon
        transitive_dependencies: All transitive dependencies
        dependency_depth: Maximum dependency depth
        circular_dependencies: Components in circular dependency chains
        coupling_strength: Overall coupling strength assessment
        dependents: Components that depend on this component
    """

    component: str
    direct_dependencies: List[str] = field(default_factory=list)
    transitive_dependencies: List[str] = field(default_factory=list)
    dependency_depth: int = 0
    circular_dependencies: List[str] = field(default_factory=list)
    coupling_strength: CouplingStrength = CouplingStrength.LOW
    dependents: List[str] = field(default_factory=list)

    def get_all_dependencies(self) -> List[str]:
        """Get all dependencies (direct + transitive) without duplicates."""
        all_deps = set(self.direct_dependencies + self.transitive_dependencies)
        return list(all_deps)

    def to_dict(self) -> Dict[str, Any]:
        """Convert dependency analysis to dictionary representation."""
        return {
            "component": self.component,
            "direct_dependencies": self.direct_dependencies,
            "transitive_dependencies": self.transitive_dependencies,
            "dependency_depth": self.dependency_depth,
            "circular_dependencies": self.circular_dependencies,
            "coupling_strength": self.coupling_strength.value,
            "dependents": self.dependents,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DependencyAnalysis":
        """Create dependency analysis from dictionary representation."""
        return cls(
            component=data["component"],
            direct_dependencies=data.get("direct_dependencies", []),
            transitive_dependencies=data.get("transitive_dependencies", []),
            dependency_depth=data.get("dependency_depth", 0),
            circular_dependencies=data.get("circular_dependencies", []),
            coupling_strength=CouplingStrength(data.get("coupling_strength", "low")),
            dependents=data.get("dependents", []),
        )


class ParseDiagnosticKind(Enum):
    """Kind of defect found in a parsed tree, counted separately never summed.

    UNTRUSTED_NAME is this parser's own refusal to name a declaration, not
    something the grammar reported, so it is a third kind rather than an error.
    """

    ERROR = "error"
    MISSING = "missing"
    UNTRUSTED_NAME = "untrusted_name"


@dataclass(frozen=True)
class ParseDiagnostic:
    """One structural defect in a parsed tree: kind, file, line, column, byte.

    Recorded for every language and never raised; the parse completes and returns
    its components as before. ``detail`` says why a refusal happened, in a phrase.
    """

    file_path: Path
    kind: ParseDiagnosticKind
    line: int
    column: int
    byte_offset: int
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert diagnostic to dictionary representation."""
        data: Dict[str, Any] = {
            "file_path": str(self.file_path),
            "kind": self.kind.value,
            "line": self.line,
            "column": self.column,
            "byte_offset": self.byte_offset,
        }
        if self.detail is not None:
            data["detail"] = self.detail
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParseDiagnostic":
        """Create diagnostic from dictionary representation."""
        return cls(
            file_path=Path(data["file_path"]),
            kind=ParseDiagnosticKind(data["kind"]),
            line=data["line"],
            column=data["column"],
            byte_offset=data["byte_offset"],
            detail=data.get("detail"),
        )


@dataclass
class ParseFailureReport:
    """The per-file aggregate a verification run reads, one report per file.

    The four counts are never summed: one figure cannot tell an absorbed
    extraction exception from a structural defect, which is the point of them.
    """

    file_path: Path
    language: str
    parse_error_count: int = 0
    error_node_count: int = 0
    missing_node_count: int = 0
    untrusted_name_count: int = 0

    def is_structurally_clean(self) -> bool:
        """Whether every count is zero.

        A file with no components but every count zero is clean, not a failure. A
        refused name counts here, so a refusal always earns a per-file row.
        """
        return (
            self.parse_error_count == 0
            and self.error_node_count == 0
            and self.missing_node_count == 0
            and self.untrusted_name_count == 0
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to dictionary representation."""
        return {
            "file_path": str(self.file_path),
            "language": self.language,
            "parse_error_count": self.parse_error_count,
            "error_node_count": self.error_node_count,
            "missing_node_count": self.missing_node_count,
            "untrusted_name_count": self.untrusted_name_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParseFailureReport":
        """Create report from dictionary representation."""
        return cls(
            file_path=Path(data["file_path"]),
            language=data["language"],
            parse_error_count=data.get("parse_error_count", 0),
            error_node_count=data.get("error_node_count", 0),
            missing_node_count=data.get("missing_node_count", 0),
            untrusted_name_count=data.get("untrusted_name_count", 0),
        )


@dataclass
class ParseResult:
    """
    Result of parsing a source code file.

    Attributes:
        file_path: Path to the parsed file
        language: Detected programming language
        components: List of extracted components
        parse_errors: List of parsing errors encountered
        syntax_tree: Optional syntax tree representation
        metadata: Additional parsing metadata
        diagnostics: Structural defects found, and names the parser refused.
            Additive and opt-in: a result with none serializes as before
    """

    file_path: Path
    language: str
    components: List[Component] = field(default_factory=list)
    parse_errors: List[str] = field(default_factory=list)
    syntax_tree: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[ParseDiagnostic] = field(default_factory=list)

    def has_errors(self) -> bool:
        """Check if parsing encountered any errors.

        Deliberately unchanged by the presence of diagnostics: the existing
        failed-file tally keeps its meaning and its number.
        """
        return len(self.parse_errors) > 0

    def get_component_count(self) -> int:
        """Get the number of components extracted."""
        return len(self.components)

    def failure_report(self) -> ParseFailureReport:
        """Assemble this file's count report."""
        return ParseFailureReport(
            file_path=self.file_path,
            language=self.language,
            parse_error_count=len(self.parse_errors),
            error_node_count=sum(
                1 for d in self.diagnostics if d.kind is ParseDiagnosticKind.ERROR
            ),
            missing_node_count=sum(
                1 for d in self.diagnostics if d.kind is ParseDiagnosticKind.MISSING
            ),
            untrusted_name_count=sum(
                1 for d in self.diagnostics if d.kind is ParseDiagnosticKind.UNTRUSTED_NAME
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert parse result to dictionary representation."""
        data: Dict[str, Any] = {
            "file_path": str(self.file_path),
            "language": self.language,
            "components": [comp.to_dict() for comp in self.components],
            "parse_errors": self.parse_errors,
            "metadata": self.metadata,
        }
        # Omitted when empty so a structurally clean file in any language
        # serializes byte-identically to before this field existed.
        if self.diagnostics:
            data["diagnostics"] = [d.to_dict() for d in self.diagnostics]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParseResult":
        """Create parse result from dictionary representation."""
        return cls(
            file_path=Path(data["file_path"]),
            language=data["language"],
            components=[Component.from_dict(comp) for comp in data.get("components", [])],
            parse_errors=data.get("parse_errors", []),
            metadata=data.get("metadata", {}),
            diagnostics=[ParseDiagnostic.from_dict(d) for d in data.get("diagnostics", [])],
        )


@dataclass
class KnowledgeBase:
    """
    Complete knowledge base containing all analyzed information.

    Attributes:
        source_directory: Root directory that was analyzed
        components: All discovered components
        call_graph: Component call relationships
        analysis_metadata: Metadata about the analysis process
        created_at: Timestamp when knowledge base was created
        version: Knowledge base format version
        space_id: Space identifier
        id_scheme: Component-id scheme this space was indexed with (see
            IdScheme). Empty on spaces indexed before this field existed, which
            resolves to the legacy V1 scheme so refreshes stay stable.
    """

    source_directory: Path
    components: List[Component] = field(default_factory=list)
    call_graph: Dict[str, List[str]] = field(default_factory=dict)
    analysis_metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    version: str = "1.0"
    space_id: str = ""
    id_scheme: str = ""

    def get_component_by_id(self, component_id: str) -> Optional[Component]:
        """Get a component by its ID."""
        for component in self.components:
            if component.id == component_id:
                return component
        return None

    def get_components_by_type(self, component_type: ComponentType) -> List[Component]:
        """Get all components of a specific type."""
        return [comp for comp in self.components if comp.type == component_type]

    def get_component_count(self) -> int:
        """Get total number of components."""
        return len(self.components)

    def to_dict(self) -> Dict[str, Any]:
        """Convert knowledge base to dictionary representation."""
        return {
            "source_directory": str(self.source_directory),
            "components": [comp.to_dict() for comp in self.components],
            "call_graph": self.call_graph,
            "analysis_metadata": self.analysis_metadata,
            "created_at": self.created_at,
            "version": self.version,
            "space_id": self.space_id,
            "id_scheme": self.id_scheme,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeBase":
        """Create knowledge base from dictionary representation."""
        return cls(
            source_directory=Path(data["source_directory"]),
            components=[Component.from_dict(comp) for comp in data.get("components", [])],
            call_graph=data.get("call_graph", {}),
            analysis_metadata=data.get("analysis_metadata", {}),
            created_at=data.get("created_at"),
            version=data.get("version", "1.0"),
            space_id=data.get("space_id", data.get("project_id", "")),
            id_scheme=data.get("id_scheme", ""),
        )


# ---------------------------------------------------------------------------
# Space Refresh domain models
# ---------------------------------------------------------------------------


class ChangeType(Enum):
    """Classification of a file's change status between two git commits."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    UNCHANGED = "unchanged"

    @classmethod
    def from_git_status(cls, status: str) -> "ChangeType":
        """Map a git diff --name-status code to a ChangeType.

        Supported codes:
            A        → ADDED
            M, T     → MODIFIED
            D        → REMOVED
            R<nnn>   → handled by caller as REMOVED(old) + ADDED(new)
            C<nnn>   → ADDED (copy target is new)

        Raises ValueError for unrecognized status codes.
        """
        code = status[0].upper() if status else ""
        if code == "A":
            return cls.ADDED
        if code in ("M", "T"):
            return cls.MODIFIED
        if code == "D":
            return cls.REMOVED
        if code == "R":
            # Renames are expanded by the caller into REMOVED + ADDED pairs.
            # If called directly, treat as ADDED (the new path).
            return cls.ADDED
        if code == "C":
            return cls.ADDED
        raise ValueError(f"Unrecognized git status code: {status!r}")


@dataclass(frozen=True)
class FileChange:
    """A single file's change classification in a space refresh.

    Attributes:
        path: Relative file path within the repository.
        change_type: How the file changed between the prior and new commits.
    """

    path: str
    change_type: ChangeType

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "path": self.path,
            "change_type": self.change_type.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileChange":
        """Create from dictionary representation."""
        return cls(
            path=data["path"],
            change_type=ChangeType(data["change_type"]),
        )


@dataclass
class RefreshPlan:
    """The computed plan for a space refresh operation.

    All file sets are disjoint and exhaustive over the union of the prior
    source tree and the new source tree. Component ID sets are derived from
    the file classification.

    Invariants enforced by __post_init__:
        - File sets (added, modified, removed, unchanged) are pairwise disjoint.
        - preserved_component_ids and evicted_component_ids are disjoint.

    Attributes:
        added_files: Files present in new tree but not in prior tree.
        modified_files: Files present in both trees with different content.
        removed_files: Files present in prior tree but not in new tree.
        unchanged_files: Files present in both trees with identical content.
        preserved_component_ids: Component IDs from unchanged files kept as-is.
        evicted_component_ids: Component IDs from removed/modified files to delete.
        vectors_to_delete: Component IDs whose vectors must be removed from the index.
        stale_caller_ids: Preserved component IDs whose callers need re-summarization.
        changed_component_ids: Freshly parsed component IDs from added/modified files.
        undescribed_component_ids: Unchanged IDs that left the low relevance tier undescribed.
    """

    added_files: frozenset[str] = field(default_factory=frozenset)
    modified_files: frozenset[str] = field(default_factory=frozenset)
    removed_files: frozenset[str] = field(default_factory=frozenset)
    unchanged_files: frozenset[str] = field(default_factory=frozenset)
    preserved_component_ids: frozenset[str] = field(default_factory=frozenset)
    evicted_component_ids: frozenset[str] = field(default_factory=frozenset)
    vectors_to_delete: frozenset[str] = field(default_factory=frozenset)
    stale_caller_ids: frozenset[str] = field(default_factory=frozenset)
    changed_component_ids: frozenset[str] = field(default_factory=frozenset)
    undescribed_component_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Validate disjointness invariants."""
        from ..analysis.errors import RefreshFailed

        # File sets must be pairwise disjoint
        file_sets = [
            ("added_files", self.added_files),
            ("modified_files", self.modified_files),
            ("removed_files", self.removed_files),
            ("unchanged_files", self.unchanged_files),
        ]
        for i, (name_a, set_a) in enumerate(file_sets):
            for name_b, set_b in file_sets[i + 1 :]:
                overlap = set_a & set_b
                if overlap:
                    raise RefreshFailed(
                        step="merge",
                        reason="invariant_violation",
                        detail=(
                            f"File sets {name_a} and {name_b} overlap: {sorted(list(overlap)[:5])}"
                        ),
                    )

        # Component ID sets must be disjoint
        comp_overlap = self.preserved_component_ids & self.evicted_component_ids
        if comp_overlap:
            raise RefreshFailed(
                step="merge",
                reason="invariant_violation",
                detail=(
                    f"preserved_component_ids and evicted_component_ids overlap: "
                    f"{sorted(list(comp_overlap)[:5])}"
                ),
            )

    def components_to_summarize(self) -> frozenset[str]:
        """Return the set of component IDs that need (re-)summarization.

        This is the union of freshly parsed components from changed files,
        stale callers in preserved files whose descriptions are outdated, and
        unchanged components that left the low relevance tier without a description.
        """
        return self.changed_component_ids | self.stale_caller_ids | self.undescribed_component_ids

    def is_empty(self) -> bool:
        """Return True if the refresh has no work to do (identical SHA)."""
        return (
            len(self.added_files) == 0
            and len(self.modified_files) == 0
            and len(self.removed_files) == 0
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "added_files": sorted(self.added_files),
            "modified_files": sorted(self.modified_files),
            "removed_files": sorted(self.removed_files),
            "unchanged_files": sorted(self.unchanged_files),
            "preserved_component_ids": sorted(self.preserved_component_ids),
            "evicted_component_ids": sorted(self.evicted_component_ids),
            "vectors_to_delete": sorted(self.vectors_to_delete),
            "stale_caller_ids": sorted(self.stale_caller_ids),
            "changed_component_ids": sorted(self.changed_component_ids),
            "undescribed_component_ids": sorted(self.undescribed_component_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RefreshPlan":
        """Create from dictionary representation."""
        return cls(
            added_files=frozenset(data.get("added_files", [])),
            modified_files=frozenset(data.get("modified_files", [])),
            removed_files=frozenset(data.get("removed_files", [])),
            unchanged_files=frozenset(data.get("unchanged_files", [])),
            preserved_component_ids=frozenset(data.get("preserved_component_ids", [])),
            evicted_component_ids=frozenset(data.get("evicted_component_ids", [])),
            vectors_to_delete=frozenset(data.get("vectors_to_delete", [])),
            stale_caller_ids=frozenset(data.get("stale_caller_ids", [])),
            changed_component_ids=frozenset(data.get("changed_component_ids", [])),
            undescribed_component_ids=frozenset(data.get("undescribed_component_ids", [])),
        )


@dataclass
class RefreshResult:
    """Summary of a completed space refresh operation.

    Attributes:
        files_added: Number of files classified as ADDED.
        files_modified: Number of files classified as MODIFIED.
        files_removed: Number of files classified as REMOVED.
        files_unchanged: Number of files classified as UNCHANGED.
        files_added_for_new_languages: Files added because a language became readable
        files_modified_for_part_libraries: Dart files re-keyed by a parent's `part` change
        files_excluded_generated: Changed files left out as machine-generated
        components_added: Number of new components parsed from added files.
        components_removed: Number of components evicted.
        components_reprocessed: Number of components re-parsed from modified files.
        vectors_upserted: Number of vectors written to the index.
        vectors_deleted: Number of vectors removed from the index.
        vector_errors: List of component IDs where vector operations failed.
        duration_s: Total refresh duration in seconds.
        final_error_step: Pipeline step where a fatal error occurred, or None.
        final_error_reason: Machine-readable reason for the fatal error, or None.
    """

    files_added: int = 0
    files_modified: int = 0
    files_removed: int = 0
    files_unchanged: int = 0
    files_added_for_new_languages: int = 0
    files_modified_for_part_libraries: int = 0
    files_excluded_generated: int = 0
    components_added: int = 0
    components_removed: int = 0
    components_reprocessed: int = 0
    vectors_upserted: int = 0
    vectors_deleted: int = 0
    vector_errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    final_error_step: Optional[str] = None
    final_error_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        result: Dict[str, Any] = {
            "files_added": self.files_added,
            "files_modified": self.files_modified,
            "files_removed": self.files_removed,
            "files_unchanged": self.files_unchanged,
            "components_added": self.components_added,
            "components_removed": self.components_removed,
            "components_reprocessed": self.components_reprocessed,
            "vectors_upserted": self.vectors_upserted,
            "vectors_deleted": self.vectors_deleted,
            "vector_errors": self.vector_errors,
            "duration_s": self.duration_s,
        }
        if self.final_error_step is not None:
            result["final_error_step"] = self.final_error_step
        if self.final_error_reason is not None:
            result["final_error_reason"] = self.final_error_reason
        # Additive and opt-in: a refresh that upgraded no language serializes
        # exactly as it did before this field existed.
        if self.files_added_for_new_languages:
            result["files_added_for_new_languages"] = self.files_added_for_new_languages
        # Additive and opt-in on the same terms: absent unless a split
        # library's parent changed and re-keyed part files the diff never
        # mentioned.
        if self.files_modified_for_part_libraries:
            result["files_modified_for_part_libraries"] = self.files_modified_for_part_libraries
        # Additive and opt-in on the same terms: absent unless the commit range
        # touched a file its language's patterns mark as generated.
        if self.files_excluded_generated:
            result["files_excluded_generated"] = self.files_excluded_generated
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RefreshResult":
        """Create from dictionary representation."""
        return cls(
            files_added=data.get("files_added", 0),
            files_modified=data.get("files_modified", 0),
            files_removed=data.get("files_removed", 0),
            files_unchanged=data.get("files_unchanged", 0),
            files_added_for_new_languages=data.get("files_added_for_new_languages", 0),
            files_modified_for_part_libraries=data.get("files_modified_for_part_libraries", 0),
            files_excluded_generated=data.get("files_excluded_generated", 0),
            components_added=data.get("components_added", 0),
            components_removed=data.get("components_removed", 0),
            components_reprocessed=data.get("components_reprocessed", 0),
            vectors_upserted=data.get("vectors_upserted", 0),
            vectors_deleted=data.get("vectors_deleted", 0),
            vector_errors=data.get("vector_errors", []),
            duration_s=data.get("duration_s", 0.0),
            final_error_step=data.get("final_error_step"),
            final_error_reason=data.get("final_error_reason"),
        )
