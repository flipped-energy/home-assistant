#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -d ../spec ]; then
  node ../spec/verify-vectors.ts
fi
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.14 .venv
fi
uv pip install --python .venv/bin/python --upgrade -r requirements_test.txt
.venv/bin/ruff check custom_components tests
.venv/bin/ruff format --check custom_components tests
.venv/bin/python -m pytest "$@"
.venv/bin/python -c "import homeassistant.const as c; print('tested on Home Assistant', c.__version__)"
