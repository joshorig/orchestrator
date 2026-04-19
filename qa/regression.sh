#!/usr/bin/env bash
set -euo pipefail

ROOT="${REPO_ROOT:-$(pwd)}"
cd "$ROOT"

bash qa/smoke.sh
python3 bin/orchestrator.py report morning >/dev/null
