#!/usr/bin/env python3
"""devmini orchestrator Telegram bot.

Transport: long-polling `getUpdates`. Never a webhook (no public ports per security principles).

Config: /Volumes/devssd/orchestrator/config/telegram.json (chmod 600, gitignored).
If the file is absent or clearly a placeholder, the bot logs and exits cleanly so launchd
does not respawn into an error loop.

Commands are dispatched through orchestrator.dispatch_telegram_command — shared with the
legacy file-stub poller for parity. Any unknown command returns the help string; we never
execute arbitrary shell.

Push notifications: the bot tails /Volumes/devssd/orchestrator/reports/ on startup. When a
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
    if not CONFIG_PATH.exists():
        log.error("telegram config missing: %s — copy telegram.example.json and fill in real values", CONFIG_PATH)
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("telegram config unreadable: %s", exc)
        return None
    token = cfg.get("bot_token", "")
    if not token or "REPLACE" in token.upper():
        log.error("telegram bot_token not configured (still placeholder)")
        return None
    if not cfg.get("allowed_chat_ids"):
        log.error("telegram allowed_chat_ids empty — refusing to start (would accept any caller)")
        return None
    return cfg


def log_reject(chat_id, text, reason):
    REJECT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with REJECT_LOG.open("a") as f:
        f.write(f"{dt.datetime.now().isoformat(timespec='seconds')}\t{chat_id}\t{reason}\t{text!r}\n")


def load_pushed_state():
    if not PUSHED_STATE_PATH.exists():
        return {"pushed": []}
    try:
        return json.loads(PUSHED_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"pushed": []}


def save_pushed_state(state):
    PUSHED_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PUSHED_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.rename(tmp, PUSHED_STATE_PATH)


def build_handlers(cfg):
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

    allowed = set(int(x) for x in cfg["allowed_chat_ids"])

    def gate(handler):
        async def inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id if update.effective_chat else None
            text = (update.effective_message.text or "") if update.effective_message else ""
            if chat_id is None or chat_id not in allowed:
                log_reject(chat_id, text, "chat_id_not_allowed")
                log.warning("rejected chat_id=%s text=%r", chat_id, text)
                return
            return await handler(update, ctx, text)
        return inner

    async def send_html(update, text):
        """reply_text with HTML parse mode + tag-strip fallback on parser errors."""
        try:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            log.warning("html reply failed: %s — retrying plain", exc)
            try:
                plain = re.sub(r"<[^>]+>", "", text)
                await update.effective_message.reply_text(plain)
            except Exception as exc2:
                log.warning("plain retry also failed: %s", exc2)

    def block(emoji, title, body):
        """Multi-line structured response: bold header + monospace body."""
        return f"<b>{emoji} {html_escape(title)}</b>\n<pre>{html_escape(body)}</pre>"

    async def send_block_chunked(update, emoji, title, body, body_limit=3000):
        body = body or ""
        parts = list(chunks(body, body_limit)) or [""]
        total = len(parts)
        for idx, part in enumerate(parts, start=1):
            chunk_title = title if total == 1 else f"{title} ({idx}/{total})"
            await send_html(update, block(emoji, chunk_title, part))

    async def cmd_status(update, ctx, text):
        await send_html(update, block("📊", "status", o.status_text()))

    async def cmd_queue(update, ctx, text):
        body = o.dispatch_telegram_command("/queue")
        await send_html(update, block("📋", "queue", body))

    async def cmd_tasks(update, ctx, text):
        body = o.dispatch_telegram_command("/tasks")
        await send_html(update, block("🏃", "tasks", body))

    async def cmd_task(update, ctx, text):
        body = o.dispatch_telegram_command(text)
        await send_html(update, block("🔎", "task", body))

    async def cmd_planner(update, ctx, text):
        o.tick_planner()
        await send_html(update, "✅ <b>planner</b> tick complete")

    async def cmd_reviewer(update, ctx, text):
        o.tick_reviewer()
        await send_html(update, "✅ <b>reviewer</b> tick complete")

    async def cmd_qa(update, ctx, text):
        o.tick_qa()
        await send_html(update, "✅ <b>qa</b> tick complete")

    async def cmd_cleanup(update, ctx, text):
        checked, cleaned, skipped = o.cleanup_worktrees()
        await send_html(
            update,
            (
                "✅ <b>cleanup</b> complete\n"
                f"<pre>checked={checked}\ncleaned={cleaned}\nskipped={skipped}</pre>"
            ),
        )

    async def cmd_ask(update, ctx, text):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await send_html(update, "❌ usage: <code>/ask [claude|codex|both:] &lt;question&gt;</code>")
            return
        body = o.investigate_question(parts[1].strip())
        await send_block_chunked(update, "🧭", "ask", body, body_limit=2800)

    async def cmd_regression(update, ctx, text):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_html(update, "❌ usage: <code>/regression &lt;project&gt;</code>")
            return
        project = parts[1].strip()
        try:
            o.tick_regression(project)
            await send_html(
                update,
                f"✅ regression queued for <code>{html_escape(project)}</code>",
            )
        except SystemExit as exc:
            await send_html(update, f"❌ <b>error</b>: {html_escape(str(exc))}")

    async def cmd_planner_status(update, ctx, text):
        parts = text.split(maxsplit=1)
        project = parts[1].strip() if len(parts) > 1 else None
        body = o.planner_status_text(project_filter=project)
        await send_html(update, block("📋", "planner status", body))

    async def cmd_planner_enable(update, ctx, text):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_html(update, "❌ usage: <code>/planner_enable &lt;project&gt;</code>")
            return
        project = parts[1].strip()
        cfg_names = {p["name"] for p in o.load_config()["projects"]}
        if project not in cfg_names:
            await send_html(update, f"❌ unknown project: <code>{html_escape(project)}</code>")
            return
        changed = o.set_planner_disabled(project, False)
        suffix = "" if changed else " (was already enabled)"
        await send_html(update, f"✅ planner <b>enabled</b> for <code>{html_escape(project)}</code>{suffix}")

    async def cmd_planner_disable(update, ctx, text):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await send_html(update, "❌ usage: <code>/planner_disable &lt;project&gt; [reason]</code>")
            return
        project = parts[1].strip()
        reason = parts[2].strip() if len(parts) > 2 else ""
        cfg_names = {p["name"] for p in o.load_config()["projects"]}
        if project not in cfg_names:
            await send_html(update, f"❌ unknown project: <code>{html_escape(project)}</code>")
            return
        changed = o.set_planner_disabled(project, True, reason=reason)
        suffix = f" (reason: {html_escape(reason)})" if reason else ""
        status_word = "disabled" if changed else "already disabled"
        await send_html(update, f"✅ planner <b>{status_word}</b> for <code>{html_escape(project)}</code>{suffix}")

    async def cmd_report(update, ctx, text):
        parts = text.split(maxsplit=1)
        kind = parts[1].strip() if len(parts) > 1 else "morning"
        out = o.report(kind)
        await send_html(
            update,
            f"✅ {html_escape(kind)} report written: <code>{html_escape(out.name)}</code>",
        )

    async def cmd_enqueue(update, ctx, text):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await send_html(update, "❌ usage: <code>/enqueue &lt;summary&gt;</code>")
            return
        task = o.new_task(
            role="implementer",
            engine="codex",
            project="manual",
            summary=parts[1].strip(),
            source="telegram",
        )
        o.enqueue_task(task)
        await send_html(
            update,
            f"✅ enqueued: <code>{html_escape(task['task_id'])}</code>",
        )

    async def cmd_unknown(update, ctx):
        chat_id = update.effective_chat.id if update.effective_chat else None
        text = (update.effective_message.text or "") if update.effective_message else ""
        if chat_id not in allowed:
            log_reject(chat_id, text, "chat_id_not_allowed")
            return
        log_reject(chat_id, text, "unknown_command")
        help_text = (
            "/status         orchestrator + queue snapshot\n"
            "/tasks          running task summary\n"
            "/task <id>      detail for one task + log tail\n"
            "/queue          sample tasks per state\n"
            "/planner        fire planner tick\n"
            "/planner_status planner state by project\n"
            "/planner_enable <p> enable planner for project\n"
            "/planner_disable <p> [reason] disable planner for project\n"
            "/reviewer       fire reviewer tick\n"
            "/qa             fire qa smoke tick\n"
            "/cleanup        run cleanup-worktrees now\n"
            "/ask <q>        investigate via claude\n"
            "/ask codex: <q> investigate via codex\n"
            "/ask both: <q>  query both then synthesize\n"
            "/regression <p> queue full regression sweep for project\n"
            "/report <kind>  write morning|evening status report\n"
            "/enqueue <sum>  manual codex task with summary"
        )
        await send_html(update, block("❓", "unknown command", help_text))

    async def push_reports_job(ctx):
        if not cfg.get("push_reports", True):
            return
        state = load_pushed_state()
        pushed = set(state.get("pushed", []))
        o.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        new = []
        for p in sorted(o.REPORT_DIR.glob("*.md")):
            if p.name in pushed:
                continue
            body = p.read_text()
            msg, use_html = format_report_message(p.name, body)
            for chat_id in allowed:
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode=(ParseMode.HTML if use_html else None),
                    )
                except Exception as exc:
                    log.warning("push failed to %s: %s — retrying plain text", chat_id, exc)
                    try:
                        fallback, _ = format_report_message(p.name, body, plain=True)
                        await ctx.bot.send_message(chat_id=chat_id, text=fallback)
                    except Exception as exc2:
                        log.warning("push plain retry failed to %s: %s", chat_id, exc2)
            new.append(p.name)
        if new:
            pushed.update(new)
            state["pushed"] = sorted(pushed)
            save_pushed_state(state)

    app = Application.builder().token(cfg["bot_token"]).build()
    app.add_handler(CommandHandler("status", gate(cmd_status)))
    app.add_handler(CommandHandler("tasks", gate(cmd_tasks)))
    app.add_handler(CommandHandler("task", gate(cmd_task)))
    app.add_handler(CommandHandler("queue", gate(cmd_queue)))
    app.add_handler(CommandHandler("planner", gate(cmd_planner)))
    app.add_handler(CommandHandler("planner_status", gate(cmd_planner_status)))
    app.add_handler(CommandHandler("planner_enable", gate(cmd_planner_enable)))
    app.add_handler(CommandHandler("planner_disable", gate(cmd_planner_disable)))
    app.add_handler(CommandHandler("reviewer", gate(cmd_reviewer)))
    app.add_handler(CommandHandler("qa", gate(cmd_qa)))
    app.add_handler(CommandHandler("cleanup", gate(cmd_cleanup)))
    app.add_handler(CommandHandler("ask", gate(cmd_ask)))
    app.add_handler(CommandHandler("regression", gate(cmd_regression)))
    app.add_handler(CommandHandler("report", gate(cmd_report)))
    app.add_handler(CommandHandler("enqueue", gate(cmd_enqueue)))
    # Catch-all for anything else.
    app.add_handler(MessageHandler(filters.ALL, cmd_unknown))

    # Run push poll every 60s.
    app.job_queue.run_repeating(push_reports_job, interval=60, first=5)

    return app


def chunks(s, n):
    for i in range(0, len(s), n):
        yield s[i : i + n]


REPORT_BODY_LIMIT = 3600  # ~4096 cap minus header + <pre> tags + truncation notice


def classify_report(name):
    """Return (emoji, human_title) for a report filename."""
    if name.startswith("regression-failure_"):
        return "🚨", "REGRESSION FAILURE"
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
    log.info("devmini telegram bot starting (polling mode, %d allowed chat(s))", len(cfg["allowed_chat_ids"]))
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
