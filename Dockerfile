# ─────────────────────────────────────────────────────────────────────────────
# AlphaEvolve-XLS  —  Reproducible Build Environment
# Base: Ubuntu 22.04 (matches XLS CI environment exactly)
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG BAZEL_JOBS=8

LABEL project="alphaevolve-xls"
LABEL description="AlphaEvolve AI scheduling algorithm research on Google XLS"

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update -y && apt-get install -y \
    curl git vim \
    python3 python3-pip python3-dev python-is-python3 \
    python3-venv \
    libtinfo6 build-essential \
    libxml2-dev liblapack-dev libblas-dev gfortran \
    zip default-jdk \
    patch diffutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Bazelisk (manages the exact Bazel version from .bazelversion) ─────────────
RUN curl -fLO "$(curl -s \
      -H 'Accept: application/vnd.github+json' \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      https://api.github.com/repos/bazelbuild/bazelisk/releases/latest \
      | python3 -c \
      'import json,sys; assets=json.load(sys.stdin)["assets"]; \
       print(next(a["browser_download_url"] for a in assets if "linux-amd64" in a["name"]))')" \
    && chmod +x bazelisk-linux-amd64 \
    && mv bazelisk-linux-amd64 /usr/local/bin/bazel

# ── Python project dependencies ───────────────────────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# ── Node.js + Codex CLI (AI agent) ───────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @openai/codex \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Workspace structure (XLS source and project mounted at runtime) ───────────
# /workspace/xls              ← d:\final\xls      (XLS source clone)
# /workspace/alphaevolve-xls  ← d:\final\alphaevolve-xls  (this project)
# /root/.cache/bazel          ← named Docker volume (persistent Bazel cache)
RUN mkdir -p /workspace/xls /workspace/alphaevolve-xls

WORKDIR /workspace/alphaevolve-xls

# ── Build trigger script ──────────────────────────────────────────────────────
# Placed in /usr/local/bin so it runs before ENTRYPOINT regardless of cwd.
# If the codegen_main binary isn't built yet, this runs the first build.
RUN cat > /usr/local/bin/ensure_xls_built.sh << 'EOF'
#!/bin/bash
set -e
BINARY="/workspace/xls/bazel-bin/xls/tools/codegen_main"
if [ ! -f "$BINARY" ]; then
  echo "[alphaevolve] XLS not yet built. Running initial Bazel build (~2-6 hr first time)..."
  cd /workspace/xls
  bazel build -c opt \
    //xls/tools:codegen_main \
    //xls/tools:opt_main \
    //xls/tools:ir_converter_main \
    -j ${BAZEL_JOBS:-8} \
    --show_progress_rate_limit=5
  echo "[alphaevolve] XLS build complete."
else
  echo "[alphaevolve] XLS binaries already built, skipping."
fi
EOF
RUN chmod +x /usr/local/bin/ensure_xls_built.sh

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY scripts/docker_entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "run.py", "--help"]
