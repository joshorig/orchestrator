#!/usr/bin/env bash
set -euo pipefail

ROOT="${REPO_ROOT:-$(pwd)}"
cd "$ROOT"

python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py
python3 -m doctest bin/orchestrator.py
python3 bin/orchestrator.py workflow-check >/dev/null
python3 bin/orchestrator.py tick-canary-workflows >/dev/null
