#!/usr/bin/env bash
set -euo pipefail
DEV_ROOT="${DEV_ROOT:-/Volumes/devssd}"
python3 "${DEV_ROOT}/orchestrator/bin/orchestrator.py" process-telegram
