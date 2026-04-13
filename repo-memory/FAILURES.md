# devmini orchestrator — FAILURES

_Append-only record of real orchestrator failures (worker crashes, launchd restart loops, queue corruption, security incidents) and the post-mortem learnings. Not for bugs in the repos the orchestrator serves — those live in each repo's own `repo-memory/FAILURES.md`._

## Schema

```
## YYYY-MM-DD — <one-line symptom>
**Trigger:** <what made it show up>
**Root cause:** <what was actually wrong>
**Fix:** <link to commit or a short description>
**Lesson:** <what to do differently, what check to add>
```

---

_(no entries yet — the pass-1 slice verification did not surface any orchestrator-level failure. The one real failure catalogued during pass-1 lives in `lvc-standard/repo-memory/FAILURES.md` because it was about that repo, not this one.)_
