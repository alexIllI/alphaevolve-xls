#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Docker entrypoint: ensures XLS is built, then runs the given command.
# ─────────────────────────────────────────────────────────────────────────────
set -e

# Step 1: Ensure XLS binaries are present (triggers Bazel build if needed)
ensure_xls_built.sh

# Step 2: Run the actual command
exec "$@"
