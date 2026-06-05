"""
tasks.py — full pipeline chain with build fixes
Saves output to /home/ubuntu/pipeline/runs/<timestamp>/
"""

import os
import re
import json
import logging
from datetime import datetime, date
from celery import Celery, chain
from openai import OpenAI

from prompts import get_prompt
from notifier import build_pipeline_summary, send_pipeline_summary

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL = "redis://localhost:6379/0"
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
RUNS_DIR = "/home/ubuntu/pipeline/runs"

app = Celery("pipeline", broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_max_tasks_per_child=10,
)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

MODELS = {
    "research":   "perplexity/sonar",
    "brainstorm": "moonshotai/kimi-k2",
    "architect":  "anthropic/claude-sonnet-4-5",
    "build":      "anthropic/claude-sonnet-4-5",   # was kimi-k2 - now more reliable
    "review":     "anthropic/claude-haiku-4.5",
    "report":     "google/gemini-2.5-flash",
}

MAX_PROJECTS_PER_RUN = 1  # V1: exactly one project per manual/Telegram trigger.
MAX_GENERATED_FILES = 4
MAX_GENERATED_LINES = 300
FORBIDDEN_V1_TERMS = (
    "stripe", "supabase", "langchain", "playwright", "edge runtime",
    "next/server", "next/headers", "auth.js", "nextauth",
)
TELEGRAM_REQUEST_TERMS = ("telegram", "botfather", "tg bot")
TELEGRAM_BUILD_TERMS = (
    "python-telegram-bot", "telegram.ext", "api.telegram.org",
    "from telegram import", "import telegram", "telebot", "aiogram",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)
PROJECT_TYPES = {"static_web", "web_api", "telegram_bot", "cli"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def call_llm(role: str, system: str, user: str, temperature: float = 0.5, max_tokens: int = 4096) -> str:
    # Build needs more tokens to write full files without truncation
    if role == "build":
        max_tokens = 12000
    response = client.chat.completions.create(
        model=MODELS[role],
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return response.choices[0].message.content.strip()


def parse_json(raw: str):
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        cleaned = re.sub(r'[\x00-\x1f\x7f]', '', clean)
        return json.loads(cleaned)


def parse_build_output(raw: str) -> dict:
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    try:
        cleaned = re.sub(r'[\x00-\x1f\x7f]', '', clean)
        return json.loads(cleaned)
    except Exception:
        pass
    files = {}
    pattern = r'"([^"]+\.(?:py|txt|html|md|js|json|env|yaml|yml|toml|css))"\s*:\s*"((?:[^"\\]|\\.)*)"'
    matches = re.findall(pattern, clean, re.DOTALL)
    if matches:
        for fname, content in matches:
            content = (content
                .replace("\\n", "\n").replace("\\t", "\t")
                .replace('\\"', '"').replace("\\\\", "\\"))
            files[fname] = content
        return files
    raise ValueError("Build output parse failed")


def count_generated_lines(files: dict) -> int:
    return sum(len(str(content).splitlines()) for content in files.values())


def telegram_requested(topic: str) -> bool:
    topic_lower = (topic or "").lower()
    return any(term in topic_lower for term in TELEGRAM_REQUEST_TERMS)


def validate_v1_build(files: dict, *, telegram_allowed: bool = False) -> list[str]:
    issues = []
    if len(files) > MAX_GENERATED_FILES:
        issues.append(f"Generated {len(files)} files; V1 allows at most {MAX_GENERATED_FILES}.")
    line_count = count_generated_lines(files)
    if line_count > MAX_GENERATED_LINES:
        issues.append(f"Generated {line_count} lines; V1 allows at most {MAX_GENERATED_LINES}.")
    combined = "\n".join(str(content) for content in files.values())
    combined_lower = combined.lower()
    for term in FORBIDDEN_V1_TERMS:
        if term in combined_lower:
            issues.append(f"Uses forbidden V1 dependency/pattern: {term}.")
    if not telegram_allowed:
        for term in TELEGRAM_BUILD_TERMS:
            if term.lower() in combined_lower:
                issues.append("Generated a Telegram bot, but Telegram bots were not explicitly requested.")
                break
    return issues


def infer_project_type(spec: dict, files: dict | None = None) -> str:
    requested = spec.get("project_type")
    if requested in PROJECT_TYPES:
        return requested
    file_names = set((files or {}).keys()) or set(spec.get("file_structure", []))
    deploy_target = spec.get("deploy_target", "")
    combined = " ".join(file_names).lower() + " " + json.dumps(spec).lower()
    if any(term.lower() in combined for term in TELEGRAM_BUILD_TERMS):
        return "telegram_bot"
    if deploy_target == "static" or any(name.endswith(".html") for name in file_names):
        return "static_web"
    if spec.get("endpoints"):
        return "web_api"
    return "cli"


def infer_run_command(spec: dict, files: dict | None = None) -> str:
    if spec.get("run_command"):
        return spec["run_command"]
    file_names = set((files or {}).keys()) or set(spec.get("file_structure", []))
    project_type = infer_project_type(spec, files)
    if project_type == "telegram_bot":
        return "python3 main.py"
    if project_type == "static_web" and not ({"main.py", "app.py"} & file_names):
        return "python3 -m http.server 8000"
    if "main.py" in file_names:
        return "uvicorn main:app --host 0.0.0.0 --port 8000"
    if "app.py" in file_names:
        return "uvicorn app:app --host 0.0.0.0 --port 8000"
    if "package.json" in file_names:
        return "npm install && npm start"
    return "python3 main.py"


def ensure_spec_metadata(spec: dict, files: dict | None = None) -> dict:
    spec["project_type"] = infer_project_type(spec, files)
    spec["run_command"] = infer_run_command(spec, files)
    spec.setdefault("env_vars", [])
    return spec


def important_files_for_review(files: dict) -> dict:
    preferred_names = {
        "main.py", "app.py", "server.py", "index.js", "api/index.js",
        "static/index.html", "index.html", "package.json", "requirements.txt",
        "README.md", "vercel.json",
    }
    important = {}
    for name in sorted(files):
        content = str(files[name])
        is_important = name in preferred_names or name.endswith((".py", ".js", ".html", ".json", ".txt"))
        if not is_important:
            continue
        important[name] = (content[:20000] + "\n...[truncated after 20000 chars]") if len(content) > 20000 else content
    return important or files


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.task(name="pipeline.research")
def research_task(payload):
    logger.info("🔍 RESEARCH agent starting")
    topic = payload.get("topic", "AI tools, SaaS micro-products, developer utilities")
    system, user = get_prompt("research", topic=topic, date=str(date.today()))
    raw = call_llm("research", system, user, temperature=0.5)
    trends = parse_json(raw)
    payload["raw_trends"] = trends
    logger.info(f"   → {len(trends)} trends found")
    return payload


@app.task(name="pipeline.brainstorm")
def brainstorm_task(payload):
    logger.info("💡 BRAINSTORM agent starting")
    topic = payload.get("topic", "")
    system, user = get_prompt("brainstorm",
        topic=topic,
        telegram_allowed=str(telegram_requested(topic)).lower(),
        trends=json.dumps(payload["raw_trends"], indent=2),
    )
    raw = call_llm("brainstorm", system, user, temperature=0.8)
    ideas = parse_json(raw)
    payload["ideas"] = ideas[:MAX_PROJECTS_PER_RUN]
    logger.info(f"   → {len(payload['ideas'])} ideas selected (capped at {MAX_PROJECTS_PER_RUN})")
    return payload


@app.task(name="pipeline.architect")
def architect_task(payload):
    logger.info("🏗️  ARCHITECT agent starting")
    specs = []
    for i, idea in enumerate(payload["ideas"], 1):
        logger.info(f"   [{i}/{len(payload['ideas'])}] {idea['title']}")
        topic = payload.get("topic", "")
        system, user = get_prompt("architect",
            title=idea["title"],
            description=idea["description"],
            stack=idea["stack"],
            why_now=idea["why_now"],
            monetisation=idea["monetisation"],
            topic=topic,
            telegram_allowed=str(telegram_requested(topic)).lower(),
        )
        raw = call_llm("architect", system, user, temperature=0.3)
        try:
            spec = ensure_spec_metadata(parse_json(raw))
            spec["idea"] = idea
            specs.append(spec)
        except Exception as e:
            logger.warning(f"   ⚠️  Spec failed for {idea['title']}: {e}")
    payload["specs"] = specs
    logger.info(f"   → {len(specs)} specs produced")
    return payload


@app.task(name="pipeline.build")
def build_task(payload):
    logger.info("🔨 BUILD agent starting")
    builds = []
    for i, spec in enumerate(payload["specs"], 1):
        idea = spec["idea"]
        logger.info(f"   [{i}/{len(payload['specs'])}] Building {idea['title']}")
        system, user = get_prompt("build",
            title=idea["title"],
            description=idea["description"],
            stack=idea["stack"],
            file_structure=json.dumps(spec["file_structure"]),
            endpoints=json.dumps(spec.get("endpoints", [])),
            key_logic=spec["key_logic"],
            env_vars=json.dumps(spec.get("env_vars", [])),
            deploy_target=spec.get("deploy_target", "ec2"),
            project_type=spec.get("project_type", "web_api"),
            run_command=spec.get("run_command", "python3 main.py"),
            rebuild_context="",
        )
        try:
            raw = call_llm("build", system, user, temperature=0.2)
            files = parse_build_output(raw)
            ensure_spec_metadata(spec, files)
            v1_issues = validate_v1_build(files, telegram_allowed=telegram_requested(payload.get("topic", "")))
            if spec.get("project_type") == "telegram_bot" and not telegram_requested(payload.get("topic", "")):
                v1_issues.append("Spec selected project_type=telegram_bot, but Telegram bots were not explicitly requested.")
            builds.append({"spec": spec, "files": files, "approved": False, "v1_issues": v1_issues})
            if v1_issues:
                logger.warning(f"   ⚠️  Built {idea['title']} with V1 issues: {'; '.join(v1_issues)}")
            logger.info(f"   ✅ Built {idea['title']} ({count_generated_lines(files)} lines)")
        except Exception as e:
            logger.warning(f"   ❌ Build failed for {idea['title']}: {e}")
    payload["builds"] = builds
    logger.info(f"   → {len(builds)} projects built")
    return payload


@app.task(name="pipeline.review")
def review_task(payload):
    logger.info("🔎 REVIEW agent starting")
    for i, build in enumerate(payload["builds"], 1):
        title = build["spec"]["idea"]["title"]
        logger.info(f"   [{i}/{len(payload['builds'])}] Reviewing {title}")

        files_for_review = important_files_for_review(build["files"])

        system, user = get_prompt("review",
            title=title,
            stack=build["spec"]["idea"]["stack"],
            project_type=build["spec"].get("project_type", "web_api"),
            run_command=build["spec"].get("run_command", "python3 main.py"),
            env_vars=json.dumps(build["spec"].get("env_vars", [])),
            files_preview=json.dumps(files_for_review, indent=2),
        )
        try:
            raw = call_llm("review", system, user, temperature=0.1)
            result = parse_json(raw)
            v1_issues = build.get("v1_issues", [])
            if v1_issues:
                result["pass"] = False
                result["issues"] = list(result.get("issues", [])) + v1_issues
                result["verdict"] = "Rejected by V1 guardrails: " + "; ".join(v1_issues)
            build["review"] = result
            build["approved"] = bool(result.get("pass"))
            status = "✅ PASS" if build["approved"] else "❌ FAIL"
            logger.info(f"   {status}: {title}")
        except Exception as e:
            logger.warning(f"   ⚠️  Review failed for {title}: {e}")
            build["approved"] = False
    approved = sum(1 for b in payload["builds"] if b["approved"])
    logger.info(f"   → {approved}/{len(payload['builds'])} approved")
    return payload


@app.task(name="pipeline.save")
def save_task(payload):
    logger.info("💾 SAVE agent starting")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    with open(os.path.join(run_dir, "_payload.json"), "w") as f:
        json.dump(payload, f, indent=2, default=str)

    for build in payload.get("builds", []):
        title = build["spec"]["idea"]["title"]
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:40]
        status = "approved" if build["approved"] else "rejected"
        proj_dir = os.path.join(run_dir, f"{status}_{slug}")
        os.makedirs(proj_dir, exist_ok=True)

        for fname, content in build["files"].items():
            fpath = os.path.join(proj_dir, fname)
            os.makedirs(os.path.dirname(fpath) if "/" in fname else proj_dir, exist_ok=True)
            with open(fpath, "w") as f:
                f.write(content)

        if "review" in build:
            with open(os.path.join(proj_dir, "_review.json"), "w") as f:
                json.dump(build["review"], f, indent=2)

    payload["run_dir"] = run_dir
    logger.info(f"   → Saved to {run_dir}")
    return payload


@app.task(name="pipeline.report")
def report_task(payload):
    logger.info("📋 REPORT agent starting")
    report = build_pipeline_summary(payload)
    print("\n" + "="*60)
    print(report)
    print("="*60 + "\n")

    run_dir = payload.get("run_dir")
    if run_dir:
        try:
            with open(os.path.join(run_dir, "_report.txt"), "w") as f:
                f.write(report + "\n")
        except OSError as e:
            logger.warning(f"⚠️  Could not write report file: {e}")

    if send_pipeline_summary(payload):
        logger.info("📨 Telegram notification sent")
    else:
        logger.info("📭 Telegram notification skipped or failed")

    return payload


# ── Entry point ───────────────────────────────────────────────────────────────

def run_pipeline(topic=None):
    initial_payload = {"topic": topic or "AI tools, SaaS, developer utilities"}
    pipeline = chain(
        research_task.s(initial_payload),
        brainstorm_task.s(),
        architect_task.s(),
        build_task.s(),
        review_task.s(),
        save_task.s(),
        report_task.s(),
    )
    result = pipeline.apply_async()
    logger.info(f"🚀 Pipeline launched — task ID: {result.id}")
    return result


if __name__ == "__main__":
    run_pipeline()
