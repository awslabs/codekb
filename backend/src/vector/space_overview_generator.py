"""Space Overview documentation generator using Strands agent.

Reuses the existing Hopper agent tools (from codekb.agent.codekb_agent) to
autonomously explore a codebase and generate reverse engineered documentation.

Produces 4 markdown documents:
  1. Business Overview
  2. Application Architecture Overview
  3. Technology Stack
  4. Deployment Architecture

Each document is saved as a separate .md file in S3 under the space's folder.
The document catalog is stored in status.json alongside pipeline metadata.

For large codebases, the tools return compact/summarized data to stay within
the model's context window. The agent reads actual source files selectively
rather than dumping everything.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from string import Template

from strands import Agent, tool

from ..core.language_directive import LanguageDirective, resolve_language_directive
from ..core.logging_utils import get_logger
from .overview_common import (
    MERMAID_RULE,
    NO_TOP_HEADING,
    OUTPUT_ONLY,
    OverviewGeneratorBase,
    ProgressCallback,
)

logger = get_logger(__name__)

# ── Document definitions ─────────────────────────────────────────────────

OVERVIEW_DOCUMENTS: List[Dict[str, str]] = [
    {
        "key": "business_overview",
        "title": "Business Overview",
        "filename": "business_overview.md",
    },
    {
        "key": "application_architecture",
        "title": "Application Architecture Overview",
        "filename": "application_architecture.md",
    },
    {
        "key": "technology_stack",
        "title": "Technology Stack",
        "filename": "technology_stack.md",
    },
    {
        "key": "deployment_architecture",
        "title": "Deployment Architecture",
        "filename": "deployment_architecture.md",
    },
]


# ── Exploration prompt (single-pass) ─────────────────────────────────────

EXPLORATION_PROMPT = """You are a senior software engineer performing reverse-engineering of a codebase.

Your task is to gather ALL information needed to write 4 documentation documents:
1. Business Overview
2. Application Architecture Overview
3. Technology Stack
4. Deployment Architecture

You have tools to explore the codebase. Follow the multi-step discovery workflow below:

## Step 1: Multi-Package Discovery
1. **get_stats** — Get codebase size, languages, component counts.
2. **list_source_files_recursive** — Discover the full project structure (recursive tree).
3. **list_components_summary** — Get a compact inventory of all components grouped by file.

## Step 2: Understand the Business Context
4. **Read key files** — Use read_source_file to read:
   - README.md or similar documentation
   - Main entry points (main.py, app.py, index.ts, etc.)
   - API routes / controllers / handlers
   - Service layer files
   - Domain models / data models
5. **trace_flow** on 2-3 major entry points to understand business workflows.

## Step 3: Infrastructure & Deployment Discovery
6. **Read infrastructure files** — Use read_source_file to read:
   - Dockerfile, docker-compose.yml
   - CDK/Terraform/CloudFormation files
   - CI/CD configs (.github/workflows, buildspec.yml, Jenkinsfile)
   - Deployment scripts, Makefile, Procfile, serverless.yml

## Step 4: Technology Stack Discovery
7. **Read dependency/config files** — Use read_source_file to read:
   - package.json, pyproject.toml, requirements.txt, pom.xml, build.gradle,
     Cargo.toml, go.mod, Gemfile
   - Test configs, linting configs (.eslintrc, .flake8, etc.)
   - Environment configs (.env, config/*.yaml)

## Step 5: Architecture Discovery
8. **show_dependencies** on 3-5 core components to map relationships.
9. **Read architectural files** — routers, middleware, config, database setup.

## Output Format
Produce a structured exploration report with these sections:

### STATS
[Full stats output]

### FILE STRUCTURE
[Full file tree]

### COMPONENT INVENTORY
[Component summary grouped by file]

### KEY FILE CONTENTS
For each key file you read:
#### [file_path]
```
[file contents]
```

### ENTRY POINTS AND FLOWS
[For each entry point traced, describe the flow]

### DEPENDENCIES
[For each core component analyzed, list its dependencies]

## RULES
- Be EXHAUSTIVE in reading key files. Read every important file.
- Include actual file contents, not summaries.
- Use read_source_file to read files — it handles large files with line limits.
- The exploration report will be used to generate documentation WITHOUT further tool access.
- Do NOT generate any documentation yet — only gather and report raw data.
"""

# ── Document generation prompts (no tools, context-injected) ─────────────
# Each prompt is fully self-contained with the complete output format template.
# This prevents content bleed between documents — the LLM knows exactly what
# sections to produce and what to exclude. Rule/heading language shared across
# documents comes from overview_common via $-placeholders, substituted below.

_RAW_PROMPTS: Dict[str, str] = {
    "business_overview": """\
You are a senior business analyst. Using ONLY the codebase exploration data
provided by the user, produce a **Business Overview** document.

Your document must focus EXCLUSIVELY on the BUSINESS perspective:
- What the system does from a business/user standpoint
- Business transactions and workflows
- Domain terminology
- Business-level component descriptions

DO NOT include any of the following (these belong in other documents):
- System architecture diagrams or component architecture
- Technology stack details, library versions, or framework lists
- Deployment topology, infrastructure, CI/CD pipelines
- Code-level implementation details

## Output Format — $NO_TOP_HEADING

## Business Context Diagram
```mermaid
[Mermaid diagram showing the business context — actors, external systems, data flows.
This must be a BUSINESS context diagram, NOT a system architecture diagram.
Show users/actors, the system as a single box, and external integrations.]
```

## Business Description
- **Business Description**: [Overall description of what the system does from a business perspective]
- **Business Transactions**: [List each business transaction the system implements with a description]
- **Business Dictionary**: [Key domain terms used in the codebase and their meanings]

## Component Level Business Descriptions
### [Package/Module Name]
- **Purpose**: [What it does from the business perspective]
- **Responsibilities**: [Key responsibilities in business terms]

(Repeat for each significant package/module)

## RULES
- Base ALL claims on the exploration data provided — do NOT hallucinate.
- Use business language, not implementation jargon.
- Include ALL significant packages/modules in the component descriptions.
- The mermaid diagram must be a BUSINESS CONTEXT diagram (actors + system boundary), NOT a system architecture diagram.
$MERMAID_RULE
$OUTPUT_ONLY
""",
    "application_architecture": """\
You are a senior software architect. Using ONLY the codebase exploration data
provided by the user, produce an **Application Architecture Overview** document.

Your document must focus EXCLUSIVELY on the APPLICATION ARCHITECTURE:
- How the system is structured internally (packages, layers, modules)
- Component relationships and dependencies
- Data flow through the system
- Design patterns used

DO NOT include any of the following (these belong in other documents):
- Business descriptions, business transactions, or domain dictionaries
- Technology versions, library lists, or dependency manifests
- Deployment topology, infrastructure resources, CI/CD pipelines

## Output Format — $NO_TOP_HEADING

## System Overview
[High-level description of the system architecture]

## Architecture Diagram
```mermaid
[Mermaid diagram showing all packages, services, data stores, and their relationships.
This must be a SOFTWARE ARCHITECTURE diagram showing internal structure.]
```

## Component Descriptions
### [Package/Component Name]
- **Purpose**: [What it does]
- **Responsibilities**: [Key responsibilities]
- **Dependencies**: [What it depends on]
- **Type**: [Application/Infrastructure/Model/Client/Test]

(Repeat for each significant component)

## Data Flow
```mermaid
[Mermaid sequence diagram showing key workflows end-to-end]
```

## Integration Points
- **External APIs**: [List with purposes]
- **Databases / Data Stores**: [List with purposes]
- **Third-party Services**: [List with purposes]

## Design Patterns
### [Pattern Name]
- **Location**: [Where used]
- **Purpose**: [Why used]
- **Implementation**: [How implemented]

## RULES
- Base ALL claims on the exploration data provided — do NOT hallucinate.
- Show actual component names and file paths from the codebase.
- Mermaid diagrams must reflect real components found in the data.
$MERMAID_RULE
$OUTPUT_ONLY
""",
    "technology_stack": """\
You are a senior developer. Using ONLY the codebase exploration data
provided by the user, produce a **Technology Stack** document.

Your document must focus EXCLUSIVELY on the TECHNOLOGY STACK:
- Programming languages and their versions
- Frameworks, libraries, and their versions
- Build tools, test tools, linters
- Infrastructure services referenced in config files
- Key dependencies and what they're used for

DO NOT include any of the following (these belong in other documents):
- Business descriptions, business transactions, or domain dictionaries
- Application architecture diagrams or component relationship diagrams
- Deployment topology, CI/CD pipeline descriptions

## Output Format — $NO_TOP_HEADING

## Programming Languages
| Language | Version | Usage |
|----------|---------|-------|
| [Language] | [Version if detectable] | [What it's used for] |

## Frameworks & Libraries
| Framework/Library | Version | Purpose |
|-------------------|---------|---------|
| [Name] | [Version] | [What it's used for] |

## Infrastructure & Cloud Services
| Service | Purpose |
|---------|---------|
| [Service name] | [What it's used for] |

## Build & Development Tools
| Tool | Version | Purpose |
|------|---------|---------|
| [Tool] | [Version] | [What it's used for] |

## Testing Tools & Frameworks
| Tool | Version | Purpose |
|------|---------|---------|
| [Tool] | [Version] | [What it's used for] |

## Key Dependencies
### [Dependency Name]
- **Version**: [Version]
- **Purpose**: [Why it's used]
- **Usage**: [Where/how it's used in the codebase]

## RULES
- Base ALL claims on the exploration data provided — do NOT hallucinate.
- Only list technologies you can confirm from the data.
- Include version numbers when available in config files.
$OUTPUT_ONLY
""",
    "deployment_architecture": """\
You are a senior DevOps/infrastructure engineer. Using ONLY the codebase
exploration data provided by the user, produce a **Deployment Architecture** document.

Your document must focus EXCLUSIVELY on DEPLOYMENT AND INFRASTRUCTURE:
- How the application is deployed (containers, serverless, VMs)
- Infrastructure resources (compute, networking, storage)
- CI/CD pipeline configuration
- Environment configuration

DO NOT include any of the following (these belong in other documents):
- Business descriptions, business transactions, or domain dictionaries
- Application architecture or internal component structure
- Technology version tables or library dependency lists

## Output Format — $NO_TOP_HEADING

## Deployment Overview
[High-level description of how the application is deployed]

## Deployment Diagram
```mermaid
[Mermaid diagram showing deployment topology — compute, networking, storage.
This must be an INFRASTRUCTURE diagram, NOT a software architecture diagram.]
```

## Infrastructure Components
### [Component Name]
- **Type**: [CDK Stack / Terraform Module / CloudFormation / Docker / Lambda / etc.]
- **Purpose**: [What it provisions or manages]
- **Configuration**: [Key configuration details]

(Repeat for each infrastructure component)

## Compute
| Service | Type | Purpose |
|---------|------|---------|
| [Service] | [ECS/Lambda/EC2/etc.] | [What it runs] |

## Networking
| Resource | Purpose |
|----------|---------|
| [VPC/Subnet/SG/ALB/etc.] | [What it does] |

## Storage & Data
| Resource | Type | Purpose |
|----------|------|---------|
| [Resource] | [S3/DynamoDB/RDS/etc.] | [What it stores] |

## CI/CD Pipeline
[Description of the build and deployment pipeline]

## Environment Configuration
| Environment | Description |
|-------------|-------------|
| [dev/staging/prod] | [How it's configured] |

## RULES
- Base ALL claims on the exploration data provided — do NOT hallucinate.
- Only describe infrastructure you can confirm from the codebase.
- If no infrastructure code is found, state that explicitly.
- Mermaid diagrams must reflect real infrastructure found in the data.
$MERMAID_RULE
$OUTPUT_ONLY
""",
}

# Resolve the shared prompt fragments once at module load. Templates that do not
# use $MERMAID_RULE (e.g. technology_stack) simply ignore the extra mapping.
_DOC_GENERATION_PROMPTS: Dict[str, str] = {
    key: Template(raw).substitute(
        NO_TOP_HEADING=NO_TOP_HEADING,
        MERMAID_RULE=MERMAID_RULE,
        OUTPUT_ONLY=OUTPUT_ONLY,
    )
    for key, raw in _RAW_PROMPTS.items()
}


# ── Generator class ──────────────────────────────────────────────────────


class SpaceOverviewGenerator(OverviewGeneratorBase):
    """Generates Space Overview documentation using a two-phase approach.

    Phase 1 (Explore): A single Strands agent with tools explores the codebase
    once and produces a comprehensive exploration report.

    Phase 2 (Generate): Each document is generated from the exploration report
    using a plain LLM call (no tools), avoiding redundant exploration.

    Reuses the existing Hopper agent tools from codekb.agent.codekb_agent
    for the exploration phase.
    """

    def __init__(
        self,
        space_id: str,
        on_progress: Optional[ProgressCallback] = None,
        language_directive: Optional[LanguageDirective] = None,
    ):
        super().__init__(log_prefix="[space_overview]")
        self.space_id = space_id
        self.on_progress = on_progress
        self.language_directive = language_directive or resolve_language_directive(None)

    def _get_tools(self) -> list:
        """Build tools scoped to this space for the exploration agent.

        Reuses existing Hopper agent tools (read_source_file, get_stats,
        trace_flow, show_dependencies) and adds only what's missing:
        - list_components_summary: compact component inventory (vs full dump)
        - list_source_files_recursive: recursive tree with noise filtering
          (the Hopper version is single-level only)
        """
        from ..service.codekb_service import CodeKBService
        from ..agent import codekb_agent

        codekb_service = CodeKBService.instance()
        space_id = self.space_id

        # Set the Hopper agent's space context so its tools work
        codekb_agent._current_space_ids_var.set([space_id])

        # ── Compact tool: list components grouped by file ────────────────
        @tool
        def list_components_summary() -> str:
            """List all components grouped by file. Returns name and type only (compact).

            Use this to get an overview of the codebase structure. For details
            on a specific component, use read_source_file to read its file.
            """
            try:
                components = codekb_service.list_components(space_id)
            except Exception as e:
                return f"Error: {e}"

            by_file: dict[str, list[str]] = defaultdict(list)
            for c in components:
                fp = c.get("file_path", "unknown")
                name = c.get("name", c.get("id", "?"))
                ctype = c.get("type", "?")
                if "::" in name:
                    name = name.split("::", 1)[1]
                by_file[fp].append(f"  {ctype}: {name}")

            lines = [f"Total: {len(components)} components in {len(by_file)} files\n"]
            for fp in sorted(by_file):
                lines.append(f"## {fp}")
                lines.extend(sorted(by_file[fp]))
                lines.append("")
            return "\n".join(lines)

        # ── Recursive file tree (Hopper's list_source_files is single-level) ─
        @tool
        def list_source_files_recursive(directory: str = "") -> str:
            """List files in the source directory as a recursive tree, skipping noise dirs.

            Args:
                directory: Subdirectory to list (relative to project root). Empty for root.
            """
            try:
                # Indent is measured from the root, so a subdirectory listing keeps its depth.
                source_dir = codekb_service.ensure_source_local(space_id)
                target = codekb_service.source_subdir(space_id, directory)
            except ValueError:
                return f"Access denied: path traversal in '{directory}'"
            except Exception as e:
                return f"Source not available: {e}"

            if not target.is_dir():
                return f"Directory not found: '{directory}'"

            lines = []
            for root, dirs, files in os.walk(target):
                dirs[:] = [
                    d
                    for d in sorted(dirs)
                    if not d.startswith(".")
                    and d
                    not in (
                        "node_modules",
                        "__pycache__",
                        ".git",
                        "venv",
                        ".venv",
                        "dist",
                        "build",
                        ".tox",
                        ".mypy_cache",
                        ".pytest_cache",
                        "egg-info",
                    )
                ]
                rel_root = os.path.relpath(root, source_dir)
                depth = rel_root.count(os.sep)
                indent = "  " * depth
                dir_name = os.path.basename(root)
                if rel_root == ".":
                    lines.append("./")
                else:
                    lines.append(f"{indent}{dir_name}/")
                for f in sorted(files):
                    if not f.startswith("."):
                        lines.append(f"{indent}  {f}")
            return "\n".join(lines)

        return [
            list_components_summary,
            list_source_files_recursive,
            # Reuse Hopper agent tools directly
            codekb_agent.read_source_file,
            codekb_agent.list_source_files,
            codekb_agent.get_stats,
            codekb_agent.trace_flow,
            codekb_agent.show_dependencies,
        ]

    def _explore_codebase(self) -> str:
        """Phase 1: Single agent explores the codebase and returns a report.

        Returns:
            A comprehensive exploration report string.
        """
        logger.info(f"[space_overview] Phase 1: Exploring codebase for space '{self.space_id}'")
        start = time.time()

        model = self._create_model()
        agent = Agent(
            model=model,
            tools=self._get_tools(),
            system_prompt=EXPLORATION_PROMPT + f"\n\nLoaded space: {self.space_id}",
        )

        result = agent(
            f"Thoroughly explore the codebase for space '{self.space_id}'. "
            f"Use ALL available tools to gather comprehensive data about the "
            f"project structure, components, source code, dependencies, "
            f"infrastructure, and configuration. "
            f"Output the full exploration report."
        )

        self._capture_usage(result)
        report = str(result)
        elapsed = time.time() - start
        logger.info(
            f"[space_overview] Exploration complete in {elapsed:.1f}s ({len(report)} chars)"
        )
        return report

    def _generate_doc_from_context(self, doc_key: str, exploration_report: str) -> str:
        """Phase 2: Generate a single document from the exploration report.

        Uses a plain LLM call (no tools) with the exploration data injected.

        Args:
            doc_key: Document key (e.g. 'business_overview').
            exploration_report: The full exploration report from Phase 1.

        Returns:
            The generated markdown content.
        """
        gen_prompt = _DOC_GENERATION_PROMPTS.get(doc_key)
        if not gen_prompt:
            raise ValueError(f"Unknown document key: {doc_key}")

        doc_title = next((d["title"] for d in OVERVIEW_DOCUMENTS if d["key"] == doc_key), doc_key)

        # Only the document prose is localized; the exploration prompt is not.
        instruction = self.language_directive.instruction_line()
        system_prompt = f"{gen_prompt}\n{instruction}" if instruction else gen_prompt

        return self._generate_document(
            system_prompt=system_prompt,
            user_message=(
                f"Here is the complete exploration report for the codebase:\n\n"
                f"---\n{exploration_report}\n---\n\n"
                f"Generate the {doc_title} document based on this data. "
                f"Output ONLY the markdown document."
            ),
            doc_key=doc_key,
            doc_title=doc_title,
            repair_context={"space_id": self.space_id},
        )

    def generate_all(self) -> Dict[str, Any]:
        """Generate all 4 overview documents with a single exploration pass.

        Phase 1: One agent explores the codebase thoroughly (with tools).
        Phase 2: Each document is generated from the exploration report (no tools).

        Returns:
            Dict with 'documents' list and 'stats'.
        """
        # total = 1 exploration + N documents
        total = 1 + len(OVERVIEW_DOCUMENTS)
        generated = []
        failed = []

        # Phase 1: Explore (counts as step 0)
        if self.on_progress:
            self.on_progress(0, total)

        try:
            exploration_report = self._explore_codebase()
        except Exception as e:
            logger.error(f"[space_overview] Exploration failed: {e}", exc_info=True)
            # If exploration fails, nothing can be generated
            return {
                "documents": [],
                "failed": [{"key": "exploration", "error": str(e)}],
                "stats": {
                    "total": len(OVERVIEW_DOCUMENTS),
                    "generated": 0,
                    "failed": len(OVERVIEW_DOCUMENTS),
                    "token_usage": self.token_usage.to_dict(),
                    "llm_workload": self._workload_record(),
                    "region": self._bedrock_region,
                },
            }

        if self.on_progress:
            self.on_progress(1, total)

        # Phase 2: Generate each document from the exploration report
        for i, doc_def in enumerate(OVERVIEW_DOCUMENTS):
            doc_key = doc_def["key"]

            try:
                markdown = self._generate_doc_from_context(doc_key, exploration_report)
                generated.append(
                    {
                        "key": doc_key,
                        "title": doc_def["title"],
                        "filename": doc_def["filename"],
                        "content": markdown,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except Exception as e:
                logger.error(
                    f"[space_overview] Failed to generate '{doc_key}': {e}",
                    exc_info=True,
                )
                failed.append({"key": doc_key, "error": str(e)})

            if self.on_progress:
                self.on_progress(2 + i, total)

        if self.on_progress:
            self.on_progress(total, total)

        return {
            "documents": generated,
            "failed": failed,
            "stats": {
                "total": len(OVERVIEW_DOCUMENTS),
                "generated": len(generated),
                "failed": len(failed),
                "token_usage": self.token_usage.to_dict(),
                "llm_workload": self._workload_record(),
                "region": self._bedrock_region,
            },
        }
