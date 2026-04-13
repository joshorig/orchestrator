# Mobile commands

SSH from iPhone, then use:

```bash
python3 /Volumes/devssd/orchestrator/bin/orchestrator.py status
python3 /Volumes/devssd/orchestrator/bin/orchestrator.py planner
python3 /Volumes/devssd/orchestrator/bin/orchestrator.py reviewer
python3 /Volumes/devssd/orchestrator/bin/orchestrator.py qa
python3 /Volumes/devssd/orchestrator/bin/orchestrator.py report morning
python3 /Volumes/devssd/orchestrator/bin/orchestrator.py report evening
python3 /Volumes/devssd/orchestrator/bin/orchestrator.py enqueue "Investigate build failures"
tmux attach -t devmini
```
