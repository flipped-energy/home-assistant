#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
./test.sh
.venv/bin/mypy custom_components/flipped_energy
