#!/usr/bin/env bash
#
# clean.sh - Remove build outputs and dependency installs from the project.
#
# Targets:
#   Python  : __pycache__, *.pyc/*.pyo, .pytest_cache, .mypy_cache,
#             .ruff_cache, *.egg-info, .coverage, htmlcov
#   Node    : node_modules, dist, frontend build output, .vite,
#             tsconfig.tsbuildinfo
#   CDK     : cdk.out
#
# By default virtualenvs (.venv/venv) are KEPT. Pass --venv to remove them too.
#
# Usage:
#   scripts/clean.sh [-n|--dry-run] [--venv] [-h|--help]
#
set -euo pipefail

# Resolve the project root as the parent of this script's directory, so the
# script works regardless of the current working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRY_RUN=0
REMOVE_VENV=0

# Prints the header comment, so --help and the docs can't drift.
usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1 ;;
    --venv)       REMOVE_VENV=1 ;;
    -h|--help)    usage ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# Directory names to delete wherever they appear in the tree.
DIR_TARGETS=(
  "__pycache__"
  ".pytest_cache"
  ".mypy_cache"
  ".ruff_cache"
  "node_modules"
  "dist"
  "cdk.out"
  ".vite"
  "htmlcov"
)
# *.egg-info is matched by glob below (name pattern, not exact).

# File-name patterns to delete wherever they appear.
FILE_TARGETS=(
  "*.pyc"
  "*.pyo"
  ".coverage"
  "tsconfig.tsbuildinfo"
)

# Explicit build-output paths, relative to the project root. Used for outputs
# whose directory name (e.g. "build") is too generic to match tree-wide.
EXPLICIT_PATHS=(
  "frontend/build"
)

removed_count=0

remove_path() {
  local path="$1"
  if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "would remove: ${path}"
  else
    echo "removing:     ${path}"
    rm -rf "${path}"
  fi
  removed_count=$((removed_count + 1))
}

echo "Project root: ${ROOT}"
[[ ${DRY_RUN} -eq 1 ]] && echo "(dry run - nothing will be deleted)"
echo

# Build a single `find` expression. We prune matched directories so find does
# not descend into them (faster, and avoids "no such file" noise after
# deletion). .git and virtualenvs are always pruned so we never descend into
# them; virtualenvs are only *removed* when --venv is given.
prune_expr=(-name ".git" -type d)
prune_expr+=(-o -name ".venv" -type d -o -name "venv" -type d)
for d in "${DIR_TARGETS[@]}"; do
  prune_expr+=(-o -name "${d}" -type d)
done
prune_expr+=(-o -name "*.egg-info" -type d)

# Collect directory targets (prune so we match the top dir, not its children).
while IFS= read -r -d '' p; do
  base="$(basename "${p}")"
  [[ "${base}" == ".git" ]] && continue
  if [[ "${base}" == ".venv" || "${base}" == "venv" ]]; then
    [[ ${REMOVE_VENV} -eq 1 ]] && remove_path "${p}"
    continue
  fi
  remove_path "${p}"
done < <(find "${ROOT}" \( "${prune_expr[@]}" \) -prune -print0)

# Remove explicit build-output paths if they exist.
for rel in "${EXPLICIT_PATHS[@]}"; do
  abs="${ROOT}/${rel}"
  [[ -e "${abs}" ]] && remove_path "${abs}"
done

# Collect file targets, skipping anything inside .git.
file_expr=()
first=1
for f in "${FILE_TARGETS[@]}"; do
  if [[ ${first} -eq 1 ]]; then
    file_expr+=(-name "${f}")
    first=0
  else
    file_expr+=(-o -name "${f}")
  fi
done

while IFS= read -r -d '' p; do
  remove_path "${p}"
done < <(find "${ROOT}" \( -name ".git" -o -name ".venv" -o -name "venv" \) -type d -prune \
  -o -type f \( "${file_expr[@]}" \) -print0)

echo
if [[ ${DRY_RUN} -eq 1 ]]; then
  echo "Dry run complete: ${removed_count} path(s) would be removed."
else
  echo "Clean complete: ${removed_count} path(s) removed."
fi
