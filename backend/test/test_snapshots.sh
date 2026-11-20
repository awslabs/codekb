#!/bin/bash
# Run the parser regression tests against the current baseline (mock S3
# stored by the session fixture). Safe to run after every change.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

echo "🧪 Testing parser against regression baselines"
echo ""
cd "$PROJECT_ROOT"

echo "Python..."
uv run pytest tests/test_parser_integration.py::TestIntegrationRegression::test_python_regression -v

echo ""
echo "TypeScript..."
uv run pytest tests/test_parser_integration.py::TestIntegrationRegression::test_typescript_regression -v

echo ""
echo "Java..."
uv run pytest tests/test_parser_integration.py::TestJavaRegression::test_java_regression -v

echo ""
echo "✅ All regression tests passed."
