# Mobile commands

SSH from iPhone, then use:

```bash
ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
python3 "$ORCH_ROOT/bin/orchestrator.py" status
python3 "$ORCH_ROOT/bin/orchestrator.py" planner
python3 "$ORCH_ROOT/bin/orchestrator.py" reviewer
python3 "$ORCH_ROOT/bin/orchestrator.py" qa
python3 "$ORCH_ROOT/bin/orchestrator.py" report morning
python3 "$ORCH_ROOT/bin/orchestrator.py" report evening
python3 "$ORCH_ROOT/bin/orchestrator.py" enqueue "Investigate build failures"
tmux attach -t devmini
```
