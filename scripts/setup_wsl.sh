#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_wsl.sh  —  WSL-native setup (alternative to Docker)
# Run once from /mnt/d/final/alphaevolve-xls in WSL Ubuntu
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
XLS_SRC="${XLS_SRC_PATH:-/mnt/d/final/xls}"

echo "=========================================="
echo " AlphaEvolve-XLS  WSL Setup"
echo "=========================================="
echo " Project dir : $PROJECT_DIR"
echo " XLS source  : $XLS_SRC"
echo ""

# ── 1. Verify XLS source exists ───────────────────────────────────────────────
if [ ! -d "$XLS_SRC" ]; then
  echo "[ERROR] XLS source not found at $XLS_SRC"
  echo "  Set XLS_SRC_PATH env var or clone: git clone https://github.com/google/xls $XLS_SRC"
  exit 1
fi
echo "[OK] XLS source found at $XLS_SRC"

# ── 2. System dependencies ────────────────────────────────────────────────────
echo "[INFO] Installing system dependencies..."
sudo apt-get update -y
sudo apt-get install -y \
  python3 python3-pip python3-dev python3-venv python-is-python3 \
  libtinfo6 build-essential \
  patch diffutils \
  curl

# ── 3. Bazelisk ───────────────────────────────────────────────────────────────
if ! command -v bazel &> /dev/null; then
  echo "[INFO] Installing Bazelisk..."
  BAZELISK_URL=$(curl -s \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/repos/bazelbuild/bazelisk/releases/latest \
    | python3 -c \
    'import json,sys; assets=json.load(sys.stdin)["assets"]; \
     print(next(a["browser_download_url"] for a in assets if "linux-amd64" in a["name"] and not a["name"].endswith(".sha256")))')
  curl -fLo /tmp/bazelisk "$BAZELISK_URL"
  chmod +x /tmp/bazelisk
  sudo mv /tmp/bazelisk /usr/local/bin/bazel
  echo "[OK] Bazel (bazelisk) installed at /usr/local/bin/bazel"
else
  echo "[OK] Bazel already installed: $(bazel version 2>/dev/null | head -1)"
fi

# ── 4. Node.js + Codex CLI ────────────────────────────────────────────────────
if ! command -v codex &> /dev/null; then
  echo "[INFO] Installing Node.js and Codex CLI..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
  sudo npm install -g @openai/codex
  echo "[OK] Codex CLI installed: $(codex --version 2>/dev/null || echo 'check PATH')"
else
  echo "[OK] Codex CLI already installed"
fi

# ── 5. Python virtual environment ─────────────────────────────────────────────
cd "$PROJECT_DIR"

# Ensure python3-venv is installed (needed for ensurepip/pip inside the venv)
if ! python3 -c "import ensurepip" &>/dev/null; then
  echo "[INFO] Installing python3-venv..."
  sudo apt-get install -y python3-venv python3.10-venv
fi

if [ ! -f ".venv/bin/pip" ]; then
  echo "[INFO] Creating Python virtual environment..."
  python3 -m venv .venv
fi

echo "[INFO] Installing Python dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo "[OK] Python venv ready at $PROJECT_DIR/.venv"

# ── 6. Environment file ───────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/.env" ]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo ""
  echo "[ACTION REQUIRED] Edit .env and set your OPENAI_API_KEY:"
  echo "  nano $PROJECT_DIR/.env"
fi

# ── 7. Verify XLS pre-built binary (for smoke tests before Bazel build) ───────
XLS_BIN="${XLS_BIN_PATH:-/mnt/d/final/xls-v0.0.0-9840-gd53059466-linux-x64}"
if [ -f "$XLS_BIN/codegen_main" ]; then
  echo "[OK] Pre-built XLS binary found at $XLS_BIN"
  echo "     You can use this for smoke-testing while the source build is running."
fi

echo ""
echo "=========================================="
echo " Setup complete!"
echo ""
echo " To activate the environment:"
echo "   source .venv/bin/activate"
echo ""
echo " To run the experiment (uses pre-built bins until Bazel build completes):"
echo "   python run.py --input_file designs/mac/mac.x --iterations 3"
echo ""
echo " To trigger the XLS source build (one-time, ~2-6 hours):"
echo "   cd $XLS_SRC && bazel build -c opt //xls/tools:codegen_main //xls/tools:opt_main //xls/tools:ir_converter_main"
echo "=========================================="
