#!/bin/bash
set -e
cd /mnt/d/final/alphaevolve-xls
source .venv/bin/activate
python -c "import yaml, rich, jinja2, openai; print('deps OK')"
XLS_BIN=/mnt/d/final/xls-v0.0.0-9840-gd53059466-linux-x64
XLS_SRC=/mnt/d/final/xls
python run.py --input_file designs/mac/mac.x \
  --xls_src "$XLS_SRC" \
  --xls_prebuilt "$XLS_BIN" \
  --dry_run \
  --log_level INFO
