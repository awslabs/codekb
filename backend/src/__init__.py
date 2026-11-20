"""
Code Knowledge Base - AI-powered code analysis and knowledge management system.

This package provides comprehensive code analysis capabilities using Tree-sitter
parsers to extract structural information, build call graphs, and create
queryable knowledge bases from source code repositories.

Modules are imported by their full path (e.g. ``src.core.config``). This file
deliberately imports nothing: Python runs it before every ``src.*`` import, so
re-exports here would load the analyzers into every process that touches any
module.
"""
