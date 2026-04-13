#!/usr/bin/env python3
"""Generate a daily PPD markdown report from queue, log, and BRAID stats."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402

TOKEN_RE = re.compile(r'^\s*\{.*"(input_tokens|output_tokens)".*\}\s*$')


def now():
    return dt.datetime.now()


def within_last_24h(path: pathlib.Path, ref: dt.datetime) -> bool:
    return (ref - dt.datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() <= 86400


def scan_tokens(ref: dt.datetime):
    per_slot = {
        "claude": {"input_tokens": 0, "output_tokens": 0, "tasks": 0},
        "codex": {"input_tokens": 0, "output_tokens": 0, "tasks": 0},
        "qa": {"input_tokens": 0, "output_tokens": 0, "tasks": 0},
    }
    for log_path in sorted(o.LOGS_DIR.glob("*.log")):
        if not within_last_24h(log_path, ref):
            continue
        slot = None
        for line in log_path.read_text(errors="ignore").splitlines():
            lower = line.lower()
            if slot is None:
                if "claude" in lower:
                    slot = "claude"
                elif "codex" in lower:
                    slot = "codex"
                elif "qa" in lower or "smoke" in lower or "regression" in lower:
                    slot = "qa"
            if not TOKEN_RE.match(line):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            slot_name = payload.get("slot") or slot or "codex"
            if slot_name not in per_slot:
                continue
            per_slot[slot_name]["input_tokens"] += int(payload.get("input_tokens", 0) or 0)
            per_slot[slot_name]["output_tokens"] += int(payload.get("output_tokens", 0) or 0)
    for state_name in ("done", "failed"):
        for task_path in o.queue_dir(state_name).glob("*.json"):
            if not within_last_24h(task_path, ref):
                continue
            task = o.read_json(task_path, {})
            slot = task.get("engine")
            if slot in per_slot:
                per_slot[slot]["tasks"] += 1
    return per_slot


def template_rows():
    idx = o.load_braid_index()
    rows = []
    for name, entry in sorted(idx.items()):
        uses = entry.get("uses", 0)
        errs = entry.get("topology_errors", 0)
        total = uses + errs
        error_rate = (errs / total * 100.0) if total else 0.0
        rows.append((name, uses, errs, error_rate))
    return rows


def queue_rows(ref: dt.datetime):
    rows = []
    for state_name in ("done", "failed"):
        count = 0
        for task_path in o.queue_dir(state_name).glob("*.json"):
            if within_last_24h(task_path, ref):
                count += 1
        rows.append((state_name, count))
    return rows


def read_trend():
    points = []
    for path in sorted(o.REPORT_DIR.glob("ppd-*.md"))[-7:]:
        text = path.read_text(errors="ignore")
        m = re.search(r"<!-- total_ppd=([0-9.]+) -->", text)
        if not m:
            continue
        points.append((path.stem.removeprefix("ppd-"), float(m.group(1))))
    return points


def build_report(ref: dt.datetime):
    rows = template_rows()
    per_slot = scan_tokens(ref)
    done_count = next((count for state_name, count in queue_rows(ref) if state_name == "done"), 0)
    total_input = sum(v["input_tokens"] for v in per_slot.values())
    total_output = sum(v["output_tokens"] for v in per_slot.values())
    total_tokens = total_input + total_output
    total_ppd = (done_count / total_tokens) if total_tokens else 0.0

    lines = [
        f"# PPD Report {ref.strftime('%Y-%m-%d')}",
        "",
        f"Generated: {o.now_iso()}",
        f"<!-- total_ppd={total_ppd:.8f} -->",
        "",
        "## Template Error Rate",
        "",
        "| template | uses | topology_errors | error_rate_pct |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, uses, errs, error_rate in rows:
        lines.append(f"| `{name}` | {uses} | {errs} | {error_rate:.2f} |")

    lines += [
        "",
        "## Slot Tokens",
        "",
        "| slot | input_tokens | output_tokens | tasks | tokens_per_task | ppd |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for slot in ("claude", "codex", "qa"):
        data = per_slot[slot]
        tokens = data["input_tokens"] + data["output_tokens"]
        tasks = data["tasks"]
        tpt = (tokens / tasks) if tasks else 0.0
        ppd = (tasks / tokens) if tokens else 0.0
        lines.append(
            f"| `{slot}` | {data['input_tokens']} | {data['output_tokens']} | {tasks} | {tpt:.2f} | {ppd:.8f} |"
        )

    lines += [
        "",
        "## Queue Outcome Counts",
        "",
        "| state | last_24h |",
        "| --- | ---: |",
    ]
    for state_name, count in queue_rows(ref):
        lines.append(f"| `{state_name}` | {count} |")

    lines += ["", "## Seven Day Trend", ""]
    trend = read_trend()
    if not trend:
        lines.append("- no prior PPD reports found")
    else:
        for day, value in trend:
            lines.append(f"- {day}: total_ppd={value:.8f}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ppd_report")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    ref = now()
    o.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = o.ppd_report_path(ref.strftime("%Y%m%d"))
    out.write_text(build_report(ref))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
