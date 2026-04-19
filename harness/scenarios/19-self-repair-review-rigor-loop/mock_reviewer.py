def review():
    return {
        "review_verdict": "request_change",
        "changed_files_text": "bin/orchestrator.py\nbin/worker.py\n",
        "policy_review_findings": [
            "test-to-code delta ratio too low: code_files=2 test_files=0",
        ],
    }
