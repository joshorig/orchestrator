#!/usr/bin/env bash
# Create the devmini tmux session with three windows: status, logs, shell.
# Idempotent: if the session already exists, exit 0 without changes.
# Invoked by com.devmini.orchestrator.tmux.plist at load time (no KeepAlive).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEV_ROOT="${DEV_ROOT:-$(cd "${STATE_ROOT}/.." && pwd)}"
TMUX=/opt/homebrew/bin/tmux
PY=/opt/homebrew/bin/python3
ORCH="${STATE_ROOT}/bin/orchestrator.py"
LOGS="${STATE_ROOT}/logs"

if ! [ -x "$TMUX" ]; then
    echo "FATAL: tmux not found at $TMUX" >&2
    exit 2
fi

if "$TMUX" has-session -t devmini 2>/dev/null; then
    echo "devmini session already exists"
    exit 0
fi

# Window 1: live status refresh
"$TMUX" new-session -d -s devmini -n status
"$TMUX" send-keys -t devmini:status "watch -n 5 '${PY} ${ORCH} status'" C-m

# Window 2: tail all worker logs
"$TMUX" new-window -t devmini -n logs
"$TMUX" send-keys -t devmini:logs "tail -F ${LOGS}/*.log 2>/dev/null" C-m

# Window 3: free shell pane
"$TMUX" new-window -t devmini -n shell
"$TMUX" send-keys -t devmini:shell "cd ${STATE_ROOT} && clear" C-m

echo "devmini session created"
