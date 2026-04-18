#!/usr/bin/env bash
# apply_xls_patch.sh
# ──────────────────
# Applies the AlphaEvolve-XLS modifications to a fresh Google XLS clone.
# Run this ONCE after cloning XLS and BEFORE the first Bazel build.
#
# Usage:
#   bash xls_patch/apply_xls_patch.sh /path/to/xls
#
# The XLS path defaults to /mnt/d/final/xls if not provided.
#
# Tested against XLS commit: cc0570253f692c0a4d665e50e75449ffcc614f17
# Parent (upstream base):    74000b59fd39599fe5270aacad08feed5bb640bf
#
# If your XLS clone is at a different commit, the file copy will still work
# but some context may differ. Check the diff afterwards with:
#   cd <xls_src> && git diff HEAD

set -euo pipefail

XLS_SRC="${1:-/mnt/d/final/xls}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FILES_DIR="$SCRIPT_DIR/files"

# ── Validate paths ────────────────────────────────────────────────────────────
if [[ ! -d "$XLS_SRC" ]]; then
    echo "ERROR: XLS source directory not found: $XLS_SRC"
    echo "Usage: bash xls_patch/apply_xls_patch.sh /path/to/your/xls"
    exit 1
fi

if [[ ! -d "$FILES_DIR" ]]; then
    echo "ERROR: Patch files directory not found: $FILES_DIR"
    echo "Make sure you are running this from the alphaevolve-xls repo root."
    exit 1
fi

echo "Applying AlphaEvolve-XLS modifications to: $XLS_SRC"
echo ""

# ── Copy modified files ───────────────────────────────────────────────────────
FILES=(
    "xls/scheduling/BUILD"
    "xls/scheduling/agent_generated_scheduler.cc"
    "xls/scheduling/agent_generated_scheduler.h"
    "xls/scheduling/run_pipeline_schedule.cc"
    "xls/scheduling/scheduling_options.cc"
    "xls/scheduling/scheduling_options.h"
    "xls/tools/scheduling_options_flags.cc"
    "xls/tools/scheduling_options_flags.proto"
)

for f in "${FILES[@]}"; do
    src="$FILES_DIR/$f"
    dst="$XLS_SRC/$f"
    if [[ ! -f "$src" ]]; then
        echo "ERROR: Missing patch file: $src"
        exit 1
    fi
    echo "  copying $f"
    cp "$src" "$dst"
done

echo ""
echo "All files applied successfully."
echo ""
echo "── Next steps ───────────────────────────────────────────────────────────"
echo ""
echo "1. Build the minimum targets (needed for --ppa_mode fast):"
echo ""
echo "   cd $XLS_SRC"
echo "   bazel build -c opt \\"
echo "     //xls/scheduling:agent_generated_scheduler \\"
echo "     //xls/tools:codegen_main \\"
echo "     //xls/tools:opt_main \\"
echo "     //xls/dslx/ir_convert:ir_converter_main"
echo ""
echo "2. Also build benchmark_main for --ppa_mode slow"
echo "   (required for proc designs like mac.x, and for ASAP7 area metrics):"
echo ""
echo "   bazel build -c opt //xls/dev_tools:benchmark_main"
echo ""
echo "3. Validate the pipeline with a dry run:"
echo ""
echo "   cd <alphaevolve-xls>"
echo "   python run.py \\"
echo "     --input_file designs/dot_product/dot.x \\"
echo "     --clock_period 1000 \\"
echo "     --dry_run \\"
echo "     --xls_src $XLS_SRC"
