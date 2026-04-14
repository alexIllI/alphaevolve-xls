#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_docker.sh  —  Helper to run AlphaEvolve inside Docker
# Usage:
#   ./scripts/run_docker.sh                           # interactive shell
#   ./scripts/run_docker.sh python run.py --help      # single command
#   ./scripts/run_docker.sh python run.py \
#     --input_file designs/mac/mac.x --iterations 20
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env if present
if [ -f "$PROJECT_DIR/.env" ]; then
  set -a; source "$PROJECT_DIR/.env"; set +a
fi

cd "$PROJECT_DIR"

if [ $# -eq 0 ]; then
  # No args → drop into interactive bash
  docker-compose run --rm evolve bash
else
  docker-compose run --rm evolve "$@"
fi
