def review():
    return {
        "review_verdict": "request_change",
        "changed_files_text": "bin/orchestrator.py\nbin/worker.py\n",
        "policy_review_findings": [
            "validation evidence missing for high-risk change: code_files=2 evidence_files=0",
        ],
    }
