#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEV_ROOT="${DEV_ROOT:-$(cd "${STATE_ROOT}/.." && pwd)}"
python3 "${DEV_ROOT}/orchestrator/bin/orchestrator.py" process-telegram
