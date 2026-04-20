#!/usr/bin/env python3
"""devmini orchestrator Telegram bot.

Transport: long-polling `getUpdates`. Never a webhook (no public ports per security principles).

Config: config/telegram.json beneath the orchestrator repo root (gitignored local override).
Bot token can be provided there or via the file-backed `telegram-bot-token` secret managed by orchestrator.
Approved chats come from the orchestrator's dynamic allowlist state.

Commands are dispatched through orchestrator.dispatch_telegram_command — shared with the
legacy file-stub poller for parity. Any unknown command returns the help string; we never
execute arbitrary shell.

Push notifications: the bot tails the repo-local reports/ directory on startup. When a
new morning/evening report markdown file appears (tracked via a state file to avoid
re-pushing across restarts), push it to every allowed chat.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402

CONFIG_PATH = o.STATE_ROOT / "config" / "telegram.json"
PUSHED_STATE_PATH = o.LOGS_DIR / "telegram-pushed.json"
REJECT_LOG = o.LOGS_DIR / "telegram-reject.log"

log = logging.getLogger("devmini.telegram")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_bot_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("telegram config unreadable: %s", exc)
            return None
    token = o.load_telegram_bot_token() or cfg.get("bot_token", "")
    if not token or "REPLACE" in token.upper():
        log.error("telegram bot_token not configured")
        return None
    allowed = o.telegram_allowed_chat_ids()
    bootstrap = [int(x) for x in cfg.get("allowed_chat_ids", []) if str(x).strip()]
    if bootstrap:
        allowed = sorted(set(allowed + bootstrap))
    if not allowed:
        log.error("telegram allowlist empty — refusing to start")
        return None
    cfg["bot_token"] = token
    cfg["allowed_chat_ids"] = allowed
    return cfg


def log_reject(chat_id, text, reason):
    REJECT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with REJECT_LOG.open("a") as f:
        f.write(f"{dt.datetime.now().isoformat(timespec='seconds')}\t{chat_id}\t{reason}\t{text!r}\n")


def load_pushed_state():
    if not PUSHED_STATE_PATH.exists():
        return {"pushed": [], "workflow_check": {}}
    try:
        state = json.loads(PUSHED_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"pushed": [], "workflow_check": {}}
    state.setdefault("pushed", [])
    state.setdefault("workflow_check", {})
    return state


def save_pushed_state(state):
    PUSHED_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PUSHED_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.rename(tmp, PUSHED_STATE_PATH)


def workflow_check_fingerprint(body):
    m = re.search(r"^Fingerprint:\s*`([0-9a-f]{64})`", body, re.MULTILINE)
    return m.group(1) if m else None


def build_handlers(cfg):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ParseMode
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, ContextTypes

    bootstrap_allowed = set(int(x) for x in cfg["allowed_chat_ids"])

    def current_allowed():
        return set(o.telegram_allowed_chat_ids()) | bootstrap_allowed

    def gate(handler):
        async def inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id if update.effective_chat else None
            text = (update.effective_message.text or "") if update.effective_message else ""
            if text.startswith("/register"):
                return await handler(update, ctx, text)
            if chat_id is None or chat_id not in current_allowed():
                log_reject(chat_id, text, "chat_id_not_allowed")
                log.warning("rejected chat_id=%s text=%r", chat_id, text)
                return
            return await handler(update, ctx, text)
        return inner

    async def send_html(update, text, *, reply_markup=None):
        """reply_text with HTML parse mode + tag-strip fallback on parser errors."""
        try:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception as exc:
            log.warning("html reply failed: %s — retrying plain", exc)
            try:
                plain = re.sub(r"<[^>]+>", "", text)
                await update.effective_message.reply_text(plain, reply_markup=reply_markup)
            except Exception as exc2:
                log.warning("plain retry also failed: %s", exc2)

    def block(emoji, title, body):
        """Multi-line structured response: bold header + monospace body."""
        return f"<b>{emoji} {html_escape(title)}</b>\n<pre>{html_escape(body)}</pre>"

    async def send_to_chat(chat_id, text, *, reply_markup=None):
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception as exc:
            log.warning("send failed to %s: %s", chat_id, exc)

    def keyboard(rows):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows if row]
        )

    async def send_card(update, emoji, title, body, *, buttons=None):
        await send_html(update, block(emoji, title, body), reply_markup=(keyboard(buttons) if buttons else None))

    async def send_or_reply(update, text, *, buttons=None):
        if update is not None:
            await send_html(update, text, reply_markup=(keyboard(buttons) if buttons else None))
            return
        for chat_id in sorted(current_allowed()):
            await send_to_chat(chat_id, text, reply_markup=(keyboard(buttons) if buttons else None))

    def feature_buttons(feature):
        frontier = (feature.get("frontier") or {}).get("task_id")
        planner = (feature.get("planner") or {}).get("task_id")
        fid = feature.get("feature_id")
        first_row = [("Open feature", f"feature:{fid}")]
        if frontier:
            first_row.append(("Frontier task", f"task:{frontier}"))
        rows = [first_row]
        if planner:
            rows.append([("Planner task", f"task:{planner}")])
        action_row = []
        if frontier:
            action_row.append(("Force retry", f"action:{frontier}:retry"))
            action_row.append(("Abandon", f"action:{frontier}:abandon"))
        if action_row:
            rows.append(action_row)
        rows.append([("Abandon feature", f"action:{fid}:abandon")])
        for task_id, state in list((feature.get("child_states") or {}).items())[:4]:
            label = f"{task_id[-6:]} {state}"
            rows.append([(label[:32], f"task:{task_id}")])
        if len(feature.get("child_states") or {}) < 4:
            for task_id, state in list((feature.get("follow_up_states") or {}).items())[: 4 - len(feature.get("child_states") or {})]:
                label = f"{task_id[-6:]} {state}"
                rows.append([(label[:32], f"task:{task_id}")])
        rows.append([("Open council", f"council:{fid}")])
        return rows

    def feature_list_buttons(workflows):
        rows = []
        for wf in workflows[:8]:
            frontier = wf.get("frontier") or {}
            label = f"{(wf.get('project') or '?')[:10]} {wf.get('feature_id','')[-6:]}"
            if frontier.get("state"):
                label += f" {frontier['state']}"
            rows.append([(label[:32], f"feature:{wf.get('feature_id')}")])
        return rows or None

    def feature_text(feature):
        if not feature:
            return "feature not found"
        wf = feature if feature.get("frontier") is not None else o.feature_workflow_summary(feature)
        frontier = wf.get("frontier") or {}
        planner = wf.get("planner") or {}
        lines = [
            wf.get("feature_id") or "feature",
            f"project: {wf.get('project') or '-'}",
            f"status: {wf.get('feature_status') or '-'}",
            f"summary: {wf.get('summary') or ''}",
            f"planner: {planner.get('state') or '-'} {planner.get('task_id') or '-'}",
            f"frontier: {frontier.get('state') or '-'} {frontier.get('task_id') or '-'}",
        ]
        if frontier.get("age_text"):
            lines.append(f"frontier_age: {frontier.get('age_text')}")
        if frontier.get("attempt"):
            lines.append(f"frontier_attempt: {frontier.get('attempt')}")
        blocker = frontier.get("blocker") or {}
        if blocker.get("code"):
            lines.append(f"blocker: {blocker.get('code')}")
        child_states = wf.get("child_states") or {}
        if child_states:
            lines.append("tasks:")
            for task_id, state in list(child_states.items())[:4]:
                lines.append(f"  {task_id} {state}")
        follow_up_states = wf.get("follow_up_states") or {}
        if follow_up_states:
            lines.append("follow_up:")
            for task_id, state in list(follow_up_states.items())[:4]:
                lines.append(f"  {task_id} {state}")
        recent_events = wf.get("recent_events") or []
        if recent_events:
            lines.append("recent:")
            for evt in recent_events[:3]:
                lines.append(
                    f"  {(evt.get('ts') or '')[11:16]} {evt.get('role') or '-'}:{evt.get('event') or '-'}"
                )
        return "\n".join(lines)

    def feature_alert_text(feature):
        frontier = feature.get("frontier") or {}
        blocker = frontier.get("blocker") or {}
        task_id = frontier.get("task_id")
        lines = [
            feature.get("feature_id") or "feature",
            f"project: {feature.get('project') or '-'}",
            f"frontier: {frontier.get('state') or '-'} {task_id or '-'}",
        ]
        if frontier.get("age_text"):
            lines.append(f"age: {frontier.get('age_text')}")
        if blocker.get("code"):
            lines.append(f"blocker: {blocker.get('code')}")
        evidence = []
        if blocker.get("summary"):
            evidence.append(blocker.get("summary"))
        if blocker.get("detail"):
            evidence.append(blocker.get("detail"))
        review = None
        if task_id:
            found = o.find_task(task_id)
            task = found[1] if found else {}
            if (task or {}).get("reviewed_by"):
                review = f"reviewer={task.get('reviewed_by')}"
            unresolved = int(((feature.get("delivery") or {}).get("final_pr_sweep") or {}).get("unresolved_bot_threads") or 0)
            if unresolved:
                evidence.append(f"unresolved bot threads: {unresolved}")
            findings = list((task or {}).get("policy_review_findings") or [])[:2]
            if findings:
                evidence.extend(findings)
        if review:
            evidence.append(review)
        if evidence:
            lines.append(f"evidence: {' | '.join(str(item) for item in evidence[:3])}")
        return "\n".join(lines)

    def task_action_rows(task_id):
        found = o.find_task(task_id)
        if not found:
            return []
        state, task = found
        rows = [[("Open task", f"task:{task_id}")]]
        if state in ("blocked", "failed", "abandoned", "awaiting-review", "awaiting-qa"):
            rows.append([("Retry", f"action:{task_id}:retry")])
        if state != "done":
            rows[-1].append(("Abandon", f"action:{task_id}:abandon"))
        return rows

    def queue_buttons(state=None):
        rows = []
        for _, task in o.queue_tasks(state=state, limit=4):
            task_id = task.get("task_id")
            if not task_id:
                continue
            rows.append([(task_id[-6:], f"task:{task_id}")])
            action_row = []
            found = o.find_task(task_id)
            current_state = found[0] if found else None
            if current_state in ("blocked", "failed", "abandoned", "awaiting-review", "awaiting-qa"):
                action_row.append(("Retry", f"action:{task_id}:retry"))
            if current_state and current_state != "done":
                action_row.append(("Abandon", f"action:{task_id}:abandon"))
            if action_row:
                rows.append(action_row)
        return rows or None

    async def cmd_health(update, ctx, text):
        await send_card(update, "🟢", "health", o.telegram_health_card(), buttons=[[("Features", "cmd:/features"), ("Queue", "cmd:/queue")]])

    async def cmd_features(update, ctx, text):
        parts = text.split(maxsplit=1)
        project = parts[1].strip() if len(parts) > 1 else None
        body = o.features_brief(project)
        workflows = o.open_feature_workflow_summaries(project_name=project)
        buttons = feature_list_buttons(workflows)
        await send_card(update, "🧩", "features", body, buttons=buttons)

    async def cmd_queue(update, ctx, text):
        parts = text.split(maxsplit=1)
        state = parts[1].strip() if len(parts) > 1 else None
        await send_card(update, "📋", "queue", o.queue_brief(state), buttons=queue_buttons(state))

    async def cmd_task(update, ctx, text):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await send_html(update, "❌ usage: <code>/task &lt;task_id&gt;</code>")
            return
        await send_card(update, "🧵", "task", o.task_text(parts[1].strip()))

    async def cmd_planner(update, ctx, text):
        parts = text.split()
        if len(parts) == 1:
            await send_card(update, "🗺️", "planner", o.planner_status_text())
            return
        project = parts[1].strip()
        action = parts[2].strip().lower() if len(parts) > 2 else "status"
        if action == "status":
            await send_card(update, "🗺️", "planner", o.planner_status_text(project_filter=project))
            return
        if action == "run":
            o.tick_planner()
            await send_html(update, f"✅ <b>planner</b> tick queued for <code>{html_escape(project)}</code>")
            return
        if action == "on":
            o.set_planner_disabled(project, False)
            await send_html(update, f"✅ planner enabled for <code>{html_escape(project)}</code>")
            return
        if action == "off":
            o.set_planner_disabled(project, True, reason="telegram operator request")
            await send_html(update, f"✅ planner disabled for <code>{html_escape(project)}</code>")
            return
        await send_html(update, "❌ usage: <code>/planner &lt;project&gt; [on|off|status|run]</code>")

    def run_action(target_id, verb):
        found = o.find_task(target_id)
        if verb == "approve" and target_id.startswith("feature-"):
            o.tick_self_repair_queue()
            return f"{target_id}: self-repair queue ticked"
        if verb == "abandon" and target_id.startswith("feature-"):
            feature = o.read_feature(target_id)
            if not feature:
                return f"target not found: {target_id}"
            old_status = feature.get("status") or "open"
            o.update_feature(
                target_id,
                lambda f: f.update(
                    {
                        "status": "abandoned",
                        "abandoned_at": o.now_iso(),
                        "abandoned_reason": "telegram abandon",
                    }
                ),
            )
            o.append_transition(target_id, old_status, "abandoned", "telegram abandon")
            o.append_event(
                "telegram",
                "feature_abandoned",
                details={"feature_id": target_id, "reason": "telegram abandon"},
            )
            return f"{target_id}: {old_status} -> abandoned"
        if not found:
            return f"target not found: {target_id}"
        state, task = found
        if verb in ("retry", "unblock"):
            o.reset_task_for_retry(target_id, state, reason=f"telegram {verb}", source="telegram")
            return f"{target_id}: {state} -> queued"
        if verb == "abandon":
            o.move_task(target_id, state, "abandoned", reason="telegram abandon", mutator=lambda t: t.update({"finished_at": o.now_iso(), "abandoned_reason": "telegram abandon"}))
            return f"{target_id}: {state} -> abandoned"
        return f"unsupported action: {verb}"

    async def cmd_action(update, ctx, text):
        parts = text.split()
        if len(parts) < 3:
            await send_html(update, "❌ usage: <code>/action &lt;id&gt; &lt;retry|abandon|unblock|approve&gt;</code>")
            return
        await send_html(update, f"✅ <pre>{html_escape(run_action(parts[1], parts[2].lower()))}</pre>")

    async def cmd_ask(update, ctx, text):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await send_html(update, "❌ usage: <code>/ask [claude|codex|both:] &lt;question&gt;</code>")
            return
        body = o.investigate_question(parts[1].strip())
        full_token = None
        if len(body) > o.load_config().get("telegram", {}).get("card_limit", 600):
            full_token = o.remember_full_message(body)
        await send_card(
            update,
            "🧭",
            "ask",
            o._trim_card(body),
            buttons=[[("Read full", f"readfull:{full_token}")]] if full_token else None,
        )

    async def cmd_report(update, ctx, text):
        parts = text.split(maxsplit=1)
        kind = parts[1].strip() if len(parts) > 1 else "morning"
        out = o.report(kind)
        await send_html(update, f"✅ {html_escape(kind)} report written: <code>{html_escape(out.name)}</code>")

    async def cmd_unknown(update, ctx):
        chat_id = update.effective_chat.id if update.effective_chat else None
        text = (update.effective_message.text or "") if update.effective_message else ""
        if chat_id is None or chat_id not in current_allowed():
            log_reject(chat_id, text, "chat_id_not_allowed")
            return
        log_reject(chat_id, text, "unknown_command")
        help_text = (
            "start with /health\n\n"
            "/health\n"
            "/features [project]\n"
            "/queue [state]\n"
            "/task <task_id>\n"
            "/planner <project> [on|off|status|run]\n"
            "/action <id> <retry|abandon|unblock|approve>\n"
            "/ask <question>\n"
            "/report [morning|evening]"
        )
        await send_html(update, block("❓", "unknown command", help_text))

    async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        data = query.data or ""
        chat_id = query.message.chat_id if query.message else None
        if chat_id not in current_allowed():
            return
        if data.startswith("cmd:"):
            cmd = data.split(":", 1)[1]
            pseudo = type("Pseudo", (), {"effective_chat": query.message.chat, "effective_message": query.message})
            if cmd == "/features":
                return await cmd_features(pseudo, ctx, cmd)
            if cmd == "/queue":
                return await cmd_queue(pseudo, ctx, cmd)
        if data.startswith("task:"):
            task_id = data.split(":", 1)[1]
            await query.message.reply_text(o.task_text(task_id), reply_markup=keyboard(task_action_rows(task_id)) if task_action_rows(task_id) else None)
            return
        if data.startswith("feature:"):
            feature_id = data.split(":", 1)[1]
            feature = o.read_feature(feature_id)
            wf = o.feature_workflow_summary(feature) if feature else {}
            buttons = feature_buttons(wf) if wf else None
            await query.message.reply_text(feature_text(wf or feature), reply_markup=keyboard(buttons) if buttons else None)
            return
        if data.startswith("readfull:"):
            token = data.split(":", 1)[1]
            body = o.load_full_message(token) or "(full response expired)"
            await query.message.reply_text(body[:6000])
            return
        if data.startswith("action:"):
            _, target_id, verb = data.split(":", 2)
            await query.message.reply_text(run_action(target_id, verb))
            return
        if data.startswith("council:"):
            feature = o.read_feature(data.split(":", 1)[1]) or {}
            sr = feature.get("self_repair") or {}
            body = json.dumps(sr.get("issues") or [], indent=2, sort_keys=True)[:3500] or "(no council state)"
            await query.message.reply_text(body)

    async def push_alerts_job(ctx):
        health = o._health_payload()
        env_ok = bool(health.get("environment_ok")) and int(health.get("environment_error_count") or 0) == 0
        if (not env_ok or int(health.get("workflow_check_issue_count") or 0) > 0):
            key = (
                f"health:{int(health.get('environment_error_count') or 0)}:"
                f"{int(health.get('workflow_check_issue_count') or 0)}:"
                f"{int(health.get('feature_frontier_blocked_count') or 0)}"
            )
            if o.should_push_alert(key, 900):
                await send_or_reply(
                    None,
                    block("🩺", "health", o.telegram_health_card()),
                    buttons=[[("Features", "cmd:/features"), ("Queue", "cmd:/queue")]],
                )
        for slot, paused in o.slot_pause_status().items():
            if paused and o.should_push_alert(f"slot-paused:{slot}:{paused.get('paused_at')}", 3600):
                await send_or_reply(
                    None,
                    block("🚨", "issue", o.issue_card({"project": "global", "code": "slot_paused", "summary": paused.get("reason"), "details": {"detail": paused.get("reason")}})),
                    buttons=[[("Resume slot", f"readfull:{o.remember_full_message(f'python3 bin/orchestrator.py slots resume --slot {slot}')}" )]],
                )
        for wf in o.open_feature_workflow_summaries():
            frontier = wf.get("frontier") or {}
            if frontier.get("state") == "blocked" and (frontier.get("age_seconds") or 0) >= 2 * 3600:
                key = f"feature-blocked:{wf.get('feature_id')}:{frontier.get('task_id')}:{frontier.get('state')}"
                if o.should_push_alert(key, 1800):
                    await send_or_reply(
                        None,
                        block("⚠️", "feature blocked", feature_alert_text(wf)),
                        buttons=feature_buttons(wf),
                    )
        for path in sorted(o.REPORT_DIR.glob("workflow-check_*.md"))[-3:]:
            fingerprint = workflow_check_fingerprint(path.read_text(errors="replace"))
            if not fingerprint:
                continue
            key = f"workflow-check:{fingerprint}"
            if o.should_push_alert(key, 86400):
                await send_or_reply(
                    None,
                    block("🛠️", "workflow check", path.read_text(errors="replace")[:3200]),
                    buttons=[[("Read full", f"readfull:{o.remember_full_message(path.read_text(errors='replace'))}")]],
                )

    app = Application.builder().token(cfg["bot_token"]).build()
    app.add_handler(CommandHandler("health", gate(cmd_health)))
    app.add_handler(CommandHandler("features", gate(cmd_features)))
    app.add_handler(CommandHandler("queue", gate(cmd_queue)))
    app.add_handler(CommandHandler("task", gate(cmd_task)))
    app.add_handler(CommandHandler("planner", gate(cmd_planner)))
    app.add_handler(CommandHandler("action", gate(cmd_action)))
    app.add_handler(CommandHandler("ask", gate(cmd_ask)))
    app.add_handler(CommandHandler("report", gate(cmd_report)))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL, cmd_unknown))

    app.job_queue.run_repeating(push_alerts_job, interval=30, first=5)

    return app


def chunks(s, n):
    for i in range(0, len(s), n):
        yield s[i : i + n]


REPORT_BODY_LIMIT = 3600  # ~4096 cap minus header + <pre> tags + truncation notice


def classify_report(name):
    """Return (emoji, human_title) for a report filename."""
    if name.startswith("regression-failure_"):
        return "🚨", "REGRESSION FAILURE"
    if name.startswith("workflow-check_"):
        return "🛠️", "Workflow Check"
    if name.startswith("morning_"):
        return "📊", "Morning Report"
    if name.startswith("evening_"):
        return "📊", "Evening Report"
    return "📄", name


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_report_message(name, body, plain=False):
    """Render a report file as a single-message telegram payload.

    Returns (text, use_html). In HTML mode the body is wrapped in <pre> so
    Telegram renders the raw markdown as a monospace code block — this side-
    steps markdown-escaping footguns entirely. The header line shows the
    classification emoji + human title + filename.
    """
    emoji, title = classify_report(name)
    body_snippet = body
    truncated = len(body) > REPORT_BODY_LIMIT
    if truncated:
        body_snippet = body[:REPORT_BODY_LIMIT]
    if plain:
        suffix = f"\n… (truncated — full file: {name})" if truncated else ""
        return f"{emoji} {title}\n{name}\n\n{body_snippet}{suffix}", False
    suffix_html = ""
    if truncated:
        suffix_html = f"\n… (truncated — full file: <code>{html_escape(name)}</code>)"
    header = f"<b>{emoji} {html_escape(title)}</b>\n<i>{html_escape(name)}</i>\n"
    return f"{header}<pre>{html_escape(body_snippet)}{suffix_html}</pre>", True


def main():
    cfg = load_bot_config()
    if cfg is None:
        # Exit cleanly so launchd respawns after ThrottleInterval rather than tight-looping.
        return 0
    app = build_handlers(cfg)
    log.info("devmini telegram bot starting (polling mode, %d allowed chat(s))", len(set(o.telegram_allowed_chat_ids()) | set(cfg["allowed_chat_ids"])))
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
