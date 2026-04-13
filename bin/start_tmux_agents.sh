#!/usr/bin/env bash
# Create the devmini tmux session with three windows: status, logs, shell.
# Idempotent: if the session already exists, exit 0 without changes.
# Invoked by com.devmini.orchestrator.tmux.plist at load time (no KeepAlive).

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/Volumes/devssd}"
TMUX=/opt/homebrew/bin/tmux
PY=/opt/homebrew/bin/python3
ORCH="${DEV_ROOT}/orchestrator/bin/orchestrator.py"
LOGS="${DEV_ROOT}/orchestrator/logs"

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
"$TMUX" send-keys -t devmini:shell "cd ${DEV_ROOT} && clear" C-m

echo "devmini session created"
