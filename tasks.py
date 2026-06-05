"""
tasks.py — full pipeline chain with build fixes
Saves output to /home/ubuntu/pipeline/runs/<timestamp>/
"""

import os
import re
import json
import logging
import subprocess
import shutil
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
    "build":      "anthropic/claude-sonnet-4-5",
    "review":     "anthropic/claude-haiku-4.5",
    "report":     "google/gemini-2.5-flash",
}

MAX_PROJECTS_PER_RUN = 1
MAX_GENERATED_FILES = 6
MAX_GENERATED_LINES = 450
FORBIDDEN_V1_TERMS = (
    "stripe", "supabase", "langchain", "playwright", "edge runtime",
    "next/server", "next/headers", "auth.js", "nextauth",
    "next.js", "nextjs", "vercel dev",
    "runtime =", "export const runtime"
)
TELEGRAM_REQUEST_TERMS = ("telegram", "botfather", "tg bot")
TELEGRAM_BUILD_TERMS = (
    "python-telegram-bot", "telegram.ext", "api.telegram.org",
    "from telegram import", "import telegram", "telebot", "aiogram",
    "PROJECT_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)
PROJECT_TYPES = {"static_web", "web_api", "telegram_bot", "cli"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def call_llm(role: str, system: str, user: str, temperature: float = 0.5, max_tokens: int = 4096) -> str:
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
    """
    Robustly parse model JSON output.

    Handles:
    - markdown fenced JSON
    - text before/after JSON
    - multiple JSON-looking blocks
    - trailing text after valid JSON
    """
    import json
    import re

    if raw is None:
        raise ValueError("Empty model output")

    cleaned = str(raw).strip()
    cleaned = cleaned.replace("```json", "```").replace("```JSON", "```")

    fence_match = re.search(r"```(?:\w+)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    decoder = json.JSONDecoder()
    starts = [i for i, ch in enumerate(cleaned) if ch in "[{"]
    last_error = None

    for start in starts:
        candidate = cleaned[start:].strip()
        try:
            obj, _ = decoder.raw_decode(candidate)
            return obj
        except json.JSONDecodeError as e:
            last_error = e

    for open_ch, close_ch in [("[", "]"), ("{", "}")]:
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            candidate = cleaned[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                last_error = e

    preview = cleaned[:500].replace("\n", "\\n")
    raise ValueError(f"Could not parse JSON from model output. Last error={last_error}. Preview={preview}")


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

    if re.search(r"(?<!PROJECT_)TELEGRAM_BOT_TOKEN", combined):
        issues.append("Generated Telegram bot projects must use PROJECT_TELEGRAM_BOT_TOKEN, not TELEGRAM_BOT_TOKEN.")

    file_names = set(files.keys())
    if any(name.startswith(("pages/", "app/")) for name in file_names):
        issues.append("Generated a Next.js-style pages/app directory, which is forbidden in V1.")

    if "package.json" in file_names:
        package_text = str(files.get("package.json", "")).lower()
        if "next" in package_text or "vercel" in package_text:
            issues.append("Generated a Next.js/Vercel package.json, which is forbidden in V1.")

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


def infer_install_command(files: dict) -> str:
    file_names = set(files.keys())
    if "package.json" in file_names:
        return "npm install"
    if "requirements.txt" in file_names:
        return "python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    return "none"


def infer_test_command(files: dict) -> str:
    file_names = set(files.keys())
    if "package.json" in file_names:
        return "npm test"
    for candidate in ("main.py", "app.py", "server.py"):
        if candidate in file_names:
            return f"python -m py_compile {candidate}"
    if any(name.endswith(".html") for name in file_names):
        return "manual browser smoke test"
    return "none"


def normalized_env_vars(spec: dict) -> list[str]:
    env_vars = spec.get("env_vars") or []
    if isinstance(env_vars, str):
        try:
            parsed = json.loads(env_vars)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            pass
        return [env_vars]
    return [str(v) for v in env_vars]


def deploy_metadata(build: dict) -> dict:
    spec = build.get("spec", {})
    project_type = spec.get("project_type", "unknown")
    deployment = build.get("deployment") or {}
    live_url = deployment.get("url") or ""
    env_vars = normalized_env_vars(spec)

    if project_type == "telegram_bot":
        eligible = False
        reason = "Telegram bot projects are not auto-deployed; run them with the reported command and env vars."
    elif project_type in {"static_web", "static_site", "node_web"}:
        eligible = True
        reason = "Eligible for Vercel auto-deploy."
    elif project_type in {"web_api", "python_api"}:
        secrets = [v for v in env_vars if v and os.environ.get(str(v))]
        eligible = bool(secrets) and bool(os.environ.get("VERCEL_TOKEN"))
        reason = "API projects are auto-deployed only when required secrets and VERCEL_TOKEN are available."
    else:
        eligible = False
        reason = f"Project type {project_type} is not auto-deployed."

    if live_url:
        reason = "Deployed successfully."
    elif eligible and not deployment:
        reason = "Eligible, but no deployment result is attached to this run."
    elif deployment.get("error"):
        reason = deployment["error"]

    return {
        "eligible": eligible,
        "status": deployment.get("status", "not_deployed"),
        "live_url": live_url,
        "reason": reason,
    }


def enrich_build_report_metadata(build: dict, project_dir: str | None = None) -> dict:
    files = build.get("files", {})
    spec = ensure_spec_metadata(build.get("spec", {}), files)
    build["spec"] = spec
    build["folder_path"] = project_dir or build.get("folder_path", "")
    build["generated_files"] = sorted(files.keys())
    build["install_command"] = infer_install_command(files)
    build["test_command"] = infer_test_command(files)
    build["deploy"] = deploy_metadata(build)
    return build


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


def _extract_vercel_url(output: str) -> str:
    urls = re.findall(r"https://[^\s]+\.vercel\.app[^\s]*", output or "")
    if not urls:
        return ""
    # Prefer production URL over inspect/log URLs
    non_inspect = [u.strip().rstrip(".,)") for u in urls if "inspect" not in u.lower()]
    return (non_inspect or urls)[-1].strip().rstrip(".,)")


def _static_entry_exists(project_dir: str) -> bool:
    return (
        os.path.exists(os.path.join(project_dir, "index.html"))
        or os.path.exists(os.path.join(project_dir, "public", "index.html"))
    )


def deploy_static_build_to_vercel(build: dict) -> dict:
    """
    Deploy approved static_web projects to Vercel.

    Only handles simple static projects. Telegram bots, Python APIs, and unknown
    projects are intentionally skipped.
    """
    spec = build.get("spec", {})
    project_type = spec.get("project_type", "unknown")
    project_dir = build.get("folder_path", "")

    if not build.get("approved"):
        return {"status": "skipped", "url": "", "error": "Build not approved."}

    if project_type not in {"static_web", "static_site"}:
        return {"status": "skipped", "url": "", "error": f"Project type {project_type} is not static_web."}

    if not project_dir or not os.path.isdir(project_dir):
        return {"status": "failed", "url": "", "error": "Project folder does not exist."}

    if not _static_entry_exists(project_dir):
        return {"status": "failed", "url": "", "error": "No index.html or public/index.html found."}

    token = os.environ.get("VERCEL_TOKEN", "")
    if not token:
        return {"status": "skipped", "url": "", "error": "VERCEL_TOKEN not configured."}

    if not shutil.which("npx"):
        return {"status": "failed", "url": "", "error": "npx is not installed."}

    cmd = ["npx", "--yes", "vercel@latest", "deploy", "--prod", "--yes", "--token", token]

    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            text=True,
            capture_output=True,
            timeout=240,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "url": "", "error": "Vercel deploy timed out after 240s."}
    except Exception as exc:
        return {"status": "failed", "url": "", "error": f"Vercel deploy exception: {exc}"}

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    url = _extract_vercel_url(output)

    if result.returncode == 0 and url:
        return {"status": "deployed", "url": url, "error": ""}

    return {
        "status": "failed",
        "url": url,
        "error": f"Vercel deploy failed with code {result.returncode}: {output[-1200:]}",
    }


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
            build = enrich_build_report_metadata({"spec": spec, "files": files, "approved": False, "v1_issues": v1_issues})
            builds.append(build)
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

    for build in payload.get("builds", []):
        title = build["spec"]["idea"]["title"]
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:40]
        status = "approved" if build["approved"] else "rejected"
        proj_dir = os.path.join(run_dir, f"{status}_{slug}")
        os.makedirs(proj_dir, exist_ok=True)
        enrich_build_report_metadata(build, proj_dir)

        for fname, content in build["files"].items():
            fpath = os.path.join(proj_dir, fname)
            os.makedirs(os.path.dirname(fpath) if "/" in fname else proj_dir, exist_ok=True)
            with open(fpath, "w") as f:
                f.write(content)

        if "review" in build:
            with open(os.path.join(proj_dir, "_review.json"), "w") as f:
                json.dump(build["review"], f, indent=2)

    payload["run_dir"] = run_dir
    with open(os.path.join(run_dir, "_payload.json"), "w") as f:
        json.dump(payload, f, indent=2, default=str)
    logger.info(f"   → Saved to {run_dir}")
    return payload


@app.task(name="pipeline.deploy")
def deploy_task(payload):
    logger.info("🚀 DEPLOY agent starting")
    deployed = 0
    skipped = 0
    failed = 0

    for build in payload.get("builds", []):
        title = build.get("spec", {}).get("idea", {}).get("title", "Untitled")

        if not build.get("approved"):
            skipped += 1
            continue

        result = deploy_static_build_to_vercel(build)
        build["deployment"] = result
        enrich_build_report_metadata(build, build.get("folder_path"))

        status = result.get("status")
        if status == "deployed":
            deployed += 1
            logger.info(f"   ✅ Deployed {title}: {result.get('url')}")
        elif status == "skipped":
            skipped += 1
            logger.info(f"   ⏭️  Skipped {title}: {result.get('error')}")
        else:
            failed += 1
            logger.warning(f"   ❌ Deploy failed for {title}: {result.get('error')}")

    payload["deploy_summary"] = {
        "deployed": deployed,
        "skipped": skipped,
        "failed": failed,
    }
    logger.info(f"   → deploy summary: {payload['deploy_summary']}")
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


def run_pipeline(topic=None):
    initial_payload = {"topic": topic or "AI tools, SaaS, developer utilities"}
    pipeline = chain(
        research_task.s(initial_payload),
        brainstorm_task.s(),
        architect_task.s(),
        build_task.s(),
        review_task.s(),
        save_task.s(),
        deploy_task.s(),
        report_task.s(),
    )
    result = pipeline.apply_async()
    logger.info(f"🚀 Pipeline launched — task ID: {result.id}")
    return result


if __name__ == "__main__":
    run_pipeline()
