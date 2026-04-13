# devmini orchestrator — RESEARCH

_Pinned reading and open questions that inform architectural decisions. Not a reading list — anything in here is expected to be load-bearing for a design choice recorded in `DECISIONS.md`._

---

## BRAID — Bounded Reasoning for Autonomous Inference and Decisions

- **Cite:** Amcalar, G. & Cinar, O. (2025). _BRAID: Bounded Reasoning for Autonomous Inference and Decisions_. arXiv:2512.15959.
- **Why it matters:** The paper's generator-solver split is the direct ancestor of this orchestrator's claude/codex slot architecture. Reported 30–74× performance-per-dollar gains with matching or better accuracy versus monolithic large-model deployments.
- **Used for:** `DECISIONS.md` entry "BRAID as the structural backbone". Appendix A.1 of the paper is the source for the generator system prompt in `braid/generators/*.prompt.md`. Appendix A.4 is the source for the four graph construction principles enforced in every generator prompt.
- **Open questions:**
  - Paper §7 describes a fine-tuned Architect model that lowers generator cost further. Deferred to pass 3+.
  - Paper §7 also describes visual graph ingestion (feeding a rendered PNG to the solver). Untested here — current worker passes Mermaid source.
  - The paper treats graphs as static artifacts. Dynamic mid-run re-planning (beyond the basic topology-error → regenerate retry) is out of scope for now.

---

## Engineering memory skills (local)

- **Location:** `/Volumes/devssd/repos/skills/engineering-memory/`
- **Why it matters:** Source of the `repo-memory/` convention this file is part of. Also houses `CLAUDE.md` and `CODEX.md` policy docs that tell each LLM how to behave when called from a worker slot. When those files change, `worker.py` prompt assembly needs to be re-checked.
- **Key tools reused:** `create_agent_worktree.sh`, `cleanup_agent_worktree.sh`, `rebuild_memory_indexes.sh`, `doc_drift_report.sh`.

---

## Open research threads

- **Automated BRAID graph linter.** Currently generator output is spot-checked manually during seeding. A linter that counts tokens per node, verifies every edge has a label, and confirms terminal `Check:` nodes exist is planned for pass 2. Reference: paper Appendix A.4 rules 1–4.
- **Production PPD dashboard.** `braid/index.json` records `uses` and `topology_errors` per template. Combined with token counts from `logs/*.log` this yields a real-world analog of the paper's PPD metric. Dashboard deferred to pass 2.
- **Worker memory pressure on 16GB.** Three long-running LLM processes plus colima plus IDE is tight. The exit-then-respawn model mitigates this but has not been stress-tested under peak concurrency. No incident yet, but worth a synthetic load test before scaling slot count.
