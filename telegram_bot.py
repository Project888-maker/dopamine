"""
telegram_bot.py
Telegram bot — human interface to the dopamine pipeline.
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

from deploy_vercel import deploy_to_vercel

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
RUNS_DIR = Path("/home/ubuntu/pipeline/runs")

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def is_owner(update):
    return update.effective_user.id == OWNER_CHAT_ID


def get_latest_run():
    if not RUNS_DIR.exists():
        return None
    runs = sorted([d for d in RUNS_DIR.iterdir() if d.is_dir()], reverse=True)
    return runs[0] if runs else None


def list_projects(run_dir):
    projects = []
    for proj_dir in sorted(run_dir.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        status = "approved" if proj_dir.name.startswith("approved_") else "rejected"
        title = proj_dir.name.replace("approved_", "").replace("rejected_", "").replace("-", " ").title()
        review_file = proj_dir / "_review.json"
        verdict = ""
        if review_file.exists():
            try:
                verdict = json.loads(review_file.read_text()).get("verdict", "")
            except Exception:
                pass
        projects.append({"dir": proj_dir, "title": title, "status": status, "verdict": verdict})
    return projects


def truncate(text, n=200):
    return text[:n].rstrip() + "…" if len(text) > n else text


async def cmd_start(update, context):
    if not is_owner(update):
        await update.message.reply_text("Not authorised.")
        return
    msg = (
        "💊 *Dopamine Engine*\n\n"
        "Commands:\n"
        "/run — fire pipeline\n"
        "/list — show latest builds\n"
        "/show N — project details\n"
        "/ship N — deploy to Vercel\n"
        "/status — pipeline status"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_run(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("🏭 Pipeline launching… check back in ~10 min.")
    try:
        from tasks import run_pipeline
        result = run_pipeline()
        await update.message.reply_text(f"✅ Queued: `{result.id}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


async def cmd_list(update, context):
    if not is_owner(update):
        return
    run_dir = get_latest_run()
    if not run_dir:
        await update.message.reply_text("No runs yet. Use /run.")
        return
    projects = list_projects(run_dir)
    if not projects:
        await update.message.reply_text(f"Run {run_dir.name} has no projects.")
        return
    msg = f"🏭 *Run:* `{run_dir.name}`\n\n"
    for i, p in enumerate(projects, 1):
        emoji = "✅" if p["status"] == "approved" else "❌"
        msg += f"*{i}.* {emoji} {p['title']}\n"
        if p["verdict"]:
            msg += f"   _{truncate(p['verdict'], 100)}_\n"
        msg += "\n"
    msg += "Use `/show N` or `/ship N`"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_show(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/show N`", parse_mode="Markdown")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("N must be a number.")
        return
    run_dir = get_latest_run()
    if not run_dir:
        await update.message.reply_text("No runs yet.")
        return
    projects = list_projects(run_dir)
    if n < 1 or n > len(projects):
        await update.message.reply_text(f"Project {n} not found. Have {len(projects)}.")
        return
    p = projects[n - 1]
    readme = ""
    for fname in ["README.md", "readme.md"]:
        readme_path = p["dir"] / fname
        if readme_path.exists():
            readme = readme_path.read_text()[:800]
            break
    files = sorted([f.name for f in p["dir"].iterdir() if not f.name.startswith("_")])
    msg = f"*Project {n}: {p['title']}*\n"
    msg += f"Status: {'✅ Approved' if p['status'] == 'approved' else '❌ Rejected'}\n\n"
    if p["verdict"]:
        msg += f"*Review:* {p['verdict']}\n\n"
    if readme:
        msg += f"*README:*\n```\n{truncate(readme, 500)}\n```\n\n"
    msg += f"*Files:* {', '.join(files[:8])}"
    keyboard = []
    if p["status"] == "approved":
        keyboard.append([InlineKeyboardButton(f"🚀 Ship {n}", callback_data=f"ship:{n}")])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)


async def cmd_ship(update, context):
    if not is_owner(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/ship N`", parse_mode="Markdown")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("N must be a number.")
        return
    await _ship_project(update, n)


async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    if not is_owner(update):
        return
    data = query.data
    if data.startswith("ship:"):
        n = int(data.split(":")[1])
        await _ship_project(update, n, from_callback=True)


async def _ship_project(update, n, from_callback=False):
    run_dir = get_latest_run()
    if not run_dir:
        return
    projects = list_projects(run_dir)
    if n < 1 or n > len(projects):
        return
    p = projects[n - 1]
    if p["status"] != "approved":
        target = update.callback_query.message if from_callback else update.message
        await target.reply_text(f"⚠️ Project {n} rejected by review. Won't ship.")
        return
    target = update.callback_query.message if from_callback else update.message
    sent = await target.reply_text(f"🚀 Deploying *{p['title']}*…", parse_mode="Markdown")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        deploy_to_vercel,
        str(p["dir"]),
        f"dopamine-{p['title'].lower().replace(' ', '-')}"
    )
    if result["status"] == "deployed":
        msg = (
            f"✅ *Live!*\n\n"
            f"*{p['title']}*\n"
            f"🌐 {result['url']}\n\n"
            f"Open it. Test it."
        )
    else:
        msg = f"❌ Deploy failed: `{result['error']}`"
    await sent.edit_text(msg, parse_mode="Markdown")


async def cmd_status(update, context):
    if not is_owner(update):
        return
    run_dir = get_latest_run()
    if not run_dir:
        await update.message.reply_text("No runs yet.")
        return
    projects = list_projects(run_dir)
    approved = sum(1 for p in projects if p["status"] == "approved")
    rejected = sum(1 for p in projects if p["status"] == "rejected")
    msg = (
        f"📊 *Status*\n\n"
        f"Run: `{run_dir.name}`\n"
        f"Total: {len(projects)}\n"
        f"✅ {approved} · ❌ {rejected}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


def send_pipeline_complete_notification(payload):
    import requests
    builds = payload.get("builds", [])
    approved = [b for b in builds if b["approved"]]
    run_dir = payload.get("run_dir", "")
    run_id = os.path.basename(run_dir) if run_dir else "unknown"
    msg = f"🌅 *Pipeline complete*\n\nRun: `{run_id}`\nBuilt: {len(builds)}\nApproved: {len(approved)}\n\n"
    if approved:
        msg += "*Ready to ship:*\n"
        for i, b in enumerate(builds, 1):
            if b["approved"]:
                msg += f"  {i}. {b['spec']['idea']['title']}\n"
        msg += "\nUse `/list` and `/ship N`"
    else:
        msg += "_All failed review. Try `/run` again._"
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": OWNER_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
        timeout=10,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not OWNER_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID not set")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("ship", cmd_ship))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_handler))
    logger.info("🤖 Dopamine bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
