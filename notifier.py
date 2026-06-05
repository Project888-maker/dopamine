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


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            pass
        return [value]
    return [str(value)]


def _env_vars(spec: dict) -> str:
    return ", ".join(_as_list(spec.get("env_vars"))) or "none"


def _generated_files(build: dict) -> str:
    files = build.get("generated_files") or sorted((build.get("files") or {}).keys())
    return ", ".join(_as_list(files)) or "none"


def _deploy_line(build: dict) -> tuple[str, str, str]:
    deploy = build.get("deploy") or {}
    deployment = build.get("deployment") or {}
    eligible = deploy.get("eligible", False)
    live_url = deploy.get("live_url") or deployment.get("url") or ""
    reason = deploy.get("reason") or deployment.get("error") or "not auto-deployed"
    return ("yes" if eligible else "no", live_url or "none", reason)


def _project_line(build: dict) -> str:
    spec = build.get("spec", {})
    idea = spec.get("idea", {})
    title = idea.get("title", "Untitled")
    deploy_eligible, live_url, deploy_reason = _deploy_line(build)
    verdict = build.get("review", {}).get("verdict", "no verdict")
    return (
        f"• {title}\n"
        f"  Type: {spec.get('project_type', 'unknown')}\n"
        f"  Folder: {build.get('folder_path') or 'not saved'}\n"
        f"  Files: {_generated_files(build)}\n"
        f"  Env: {_env_vars(spec)}\n"
        f"  Install: {build.get('install_command', 'none')}\n"
        f"  Run: {spec.get('run_command', 'not specified')}\n"
        f"  Test: {build.get('test_command', 'none')}\n"
        f"  Deploy eligible: {deploy_eligible}\n"
        f"  Live URL: {live_url}\n"
        f"  Not deployed reason: {deploy_reason}\n"
        f"  Verdict: {verdict}"
    )


def build_pipeline_summary(payload: dict) -> str:
    builds = payload.get("builds", [])
    approved = [b for b in builds if b.get("approved")]
    rejected = [b for b in builds if not b.get("approved")]

    lines = [
        "🏭 Dopamine pipeline complete",
        f"Output: {payload.get('run_dir', 'N/A')}",
        f"Topic: {payload.get('topic', 'N/A')}",
        f"Built: {len(builds)} | Approved: {len(approved)} | Rejected: {len(rejected)}",
    ]

    if approved:
        lines.append("\n✅ Approved:")
        for build in approved:
            lines.append(_project_line(build))

    if rejected:
        lines.append("\n❌ Rejected:")
        for build in rejected:
            lines.append(_project_line(build))

    if not builds:
        lines.append("\nNo projects were built.")

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
