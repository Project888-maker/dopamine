"""
notifier.py — outbound notifications for pipeline results.

This module intentionally does not import telegram_bot.py so Celery workers can
send summaries without loading the interactive bot application.
"""

import json
import logging
import os
from urllib import parse, request

logger = logging.getLogger(__name__)


def build_pipeline_summary(payload: dict) -> str:
    builds = payload.get("builds", [])
    approved = [b for b in builds if b.get("approved")]
    rejected = [b for b in builds if not b.get("approved")]

    lines = [
        "🏭 Dopamine pipeline complete",
        f"Output: {payload.get('run_dir', 'N/A')}",
        f"Built: {len(builds)} | Approved: {len(approved)} | Rejected: {len(rejected)}",
    ]

    if approved:
        lines.append("\n✅ Approved:")
        for build in approved:
            lines.append(f"• {build['spec']['idea']['title']}")

    if rejected:
        lines.append("\n❌ Rejected:")
        for build in rejected:
            title = build["spec"]["idea"]["title"]
            verdict = build.get("review", {}).get("verdict", "no verdict")
            lines.append(f"• {title}: {verdict}")

    return "\n".join(lines)[:3900]


def send_telegram_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info("Telegram notification skipped; TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        req = request.Request(url, data=data, method="POST")
        with request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            logger.warning("Telegram API returned non-ok response: %s", body)
            return False
        return True
    except Exception as exc:
        logger.warning("Telegram notification failed: %s", exc)
        return False


def send_pipeline_summary(payload: dict) -> bool:
    return send_telegram_message(build_pipeline_summary(payload))
