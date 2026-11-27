# Changelog

All notable changes to CodeKB are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
CodeKB is versioned as a single unit: one version covers the backend, frontend, and
infrastructure together.

## [Unreleased]

<!-- Add entries here as changes merge to main. Group them under the headings
     below (omit any that are empty). On release, rename this section to the new
     version with a date and start a fresh Unreleased section. -->

## [1.1.0] - 2026-11-27

### Added

- Human-friendly `display_name` labels for components in the UI and REST API,
  derived from the component id (parent-qualified, with file-hash and overload
  suffixes stripped). Not persisted and not exposed to the service, MCP tools, or
  the agent, so it stays a presentation-only concern.

### Changed

- Newly minted component ids use an angle-bracket path-hash prefix
  (`stem<digest>`). Existing ids keep their legacy form across a refresh, and V1
  spaces and full-index creation are unaffected.
- Component names are now derived from the id; the separately persisted `_name`
  field was removed. Overload discriminators use a `#` separator.
- `search_components` in the MCP layer strips the `space_id::` prefix before
  matching, so patterns match ids the same way the UI does.
- Third-party log level can be set independently of `CODEKB_LOG_LEVEL`.
- Authorization allow decisions log at DEBUG; denies stay at INFO.
- Upgraded `aws-opentelemetry-distro` to 0.24.0.
- Bumped frontend dependencies (vite, react-router, katex, mermaid).

### Fixed

- `display_name` now shows in Semantic Search and Global Search results.
- Stale component details are cleared when a new search starts.
- CDK deployment validation findings.
- `setup_aws.sh` IAM policy grants `GetIndex` on the index ARN and
  `kms:DescribeKey`.
- Local setup docs and the SPA route used by the e2e test.

## [1.0.0] - Initial baseline

First tracked release. Established the baseline feature set:

- Space Overviews: generated Markdown covering a codebase's business overview,
  application architecture, technology stack, and deployment architecture.
- Semantic code search over components by intent.
- Agent Hopper chat agent grounded in analyzed code, with an Impact Analysis mode.

[Unreleased]: https://github.com/awslabs/codekb/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/awslabs/codekb/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/awslabs/codekb/releases/tag/v1.0.0
