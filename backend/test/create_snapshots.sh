#!/bin/bash
# Regenerate the parser snapshot baselines. Only run this when you have
# intentionally changed parser or flow-analyzer behavior and you want the
# new output to become the baseline.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

echo "📸 Refreshing parser snapshots"
echo ""
echo "⚠️  This overwrites the baseline in mock S3 for each language."
echo "   Only run after intentional parser behavior changes."
echo ""
cd "$PROJECT_ROOT"

uv run pytest tests/test_parser_integration.py::TestIntegrationRegression::test_save_python_snapshot -v
uv run pytest tests/test_parser_integration.py::TestIntegrationRegression::test_save_typescript_snapshot -v
uv run pytest tests/test_parser_integration.py::TestJavaRegression::test_save_java_snapshot -v

echo ""
echo "✅ Baselines saved. Run tests/test_snapshots.sh to verify."
