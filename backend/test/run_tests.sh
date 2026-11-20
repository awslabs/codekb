#!/bin/bash
# Run the backend test suite with coverage.
#
# Usage:
#   bash tests/run_tests.sh              # run everything, terminal coverage
#   bash tests/run_tests.sh --html       # also emit an HTML coverage report
#   bash tests/run_tests.sh -- -k foo    # pass extra args through to pytest
#
# Anything after `--` is forwarded to pytest, so you can still target a single
# file, class, or test (e.g. `bash tests/run_tests.sh -- tests/test_config.py`).

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

COV_REPORTS=(--cov=codekb --cov-report=term-missing)
PYTEST_EXTRA=()

while (( "$#" )); do
  case "$1" in
    --html)
      COV_REPORTS+=(--cov-report=html)
      shift
      ;;
    --)
      shift
      PYTEST_EXTRA=("$@")
      break
      ;;
    *)
      PYTEST_EXTRA+=("$1")
      shift
      ;;
  esac
done

cd "$PROJECT_ROOT"

echo "🧪 Running backend tests with coverage"
echo ""
uv run pytest "${COV_REPORTS[@]}" "${PYTEST_EXTRA[@]}"

echo ""
echo "✅ Tests complete."
if [[ " ${COV_REPORTS[*]} " == *"--cov-report=html"* ]]; then
  echo "   HTML coverage report: $PROJECT_ROOT/htmlcov/index.html"
fi
