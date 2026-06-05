"""
tasks.py — niche-based product opportunity engine pipeline
Saves output to /home/ubuntu/pipeline/runs/<timestamp>/
Pipeline: research → score → architect → ui_designer → build → review → save → deploy → report
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
from deploy_vercel import deploy_to_vercel

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL = "redis://localhost:6379/0"
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")

KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "")
KIMI_BASE_URL = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2.6")
KIMI_BUILD_ENABLED = os.environ.get("KIMI_BUILD_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
DOPAMINE_BUILD_MODEL = os.environ.get("DOPAMINE_BUILD_MODEL", "").strip()

RUNS_DIR = "/home/ubuntu/pipeline/runs"
LAST_BUILD_OUTPUT_PATH = "/tmp/dopamine_last_build_output.txt"
MAX_BUILD_DEBUG_CHARS = 200_000

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

kimi_client = OpenAI(
    base_url=KIMI_BASE_URL,
    api_key=KIMI_API_KEY or "missing",
)

MODELS = {
    "research":    "perplexity/sonar",
    "score":       "moonshotai/kimi-k2",
    "architect":   "anthropic/claude-sonnet-4-5",
    "ui_designer": "anthropic/claude-sonnet-4-5",
    "build":       "anthropic/claude-sonnet-4-5",
    "review":      "anthropic/claude-sonnet-4-5",
    "report":      "google/gemini-2.5-flash",
}
# TODO: upgrade review/build/architect to claude-sonnet-4-6 when provider ID confirmed on OpenRouter

MAX_PROJECTS_PER_RUN = 1
MAX_GENERATED_FILES = 3
MAX_GENERATED_LINES = 1200
MAX_NEXTJS_FILES = 8
MAX_NEXTJS_LINES = 800

FORBIDDEN_V1_TERMS = (
    "stripe", "supabase", "langchain", "playwright", "edge runtime",
    "next/server", "next/headers", "auth.js", "nextauth",
    "vercel dev",
    "runtime =", "export const runtime"
)
# next.js / nextjs are allowed ONLY in premium_nextjs mode
FORBIDDEN_ALWAYS_TERMS = (
    "stripe", "supabase", "langchain", "playwright", "edge runtime",
    "next/server", "next/headers", "auth.js", "nextauth",
    "vercel dev",
    "runtime =", "export const runtime"
)

TELEGRAM_REQUEST_TERMS = ("telegram", "botfather", "tg bot")
TELEGRAM_BUILD_TERMS = (
    "python-telegram-bot", "telegram.ext", "api.telegram.org",
    "from telegram import", "import telegram", "telebot", "aiogram",
    "PROJECT_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)
PROJECT_TYPES = {"static_web", "web_api", "telegram_bot", "cli", "premium_nextjs"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── LLM utilities ─────────────────────────────────────────────────────────────

def _build_model_name() -> str:
    return DOPAMINE_BUILD_MODEL or MODELS["build"]


def call_llm(role: str, system: str, user: str, temperature: float = 0.5, max_tokens: int = 4096) -> str:
    # Runtime build model is OpenRouter-managed by default.
    # Kimi direct runtime is intentionally disabled for now.
    use_kimi = False

    if role == "build":
        max_tokens = 12000

    active_client = kimi_client if use_kimi else client
    if use_kimi:
        model = KIMI_MODEL
    elif role == "build":
        model = _build_model_name()
    else:
        model = MODELS[role]
    call_temperature = 1 if use_kimi else temperature

    logger.info(f"   → LLM role={role} provider={'kimi' if use_kimi else 'openrouter'} model={model}")

    response = active_client.chat.completions.create(
        model=model,
        temperature=call_temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return response.choices[0].message.content.strip()


def parse_json(raw: str):
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


def _looks_like_html(text: str) -> bool:
    stripped = (text or "").strip().lower()
    return stripped.startswith("<!doctype html") or stripped.startswith("<html")


def _normalize_build_files(files: dict) -> dict:
    normalized = {str(name): str(content) for name, content in files.items()}
    if "index.html" not in normalized:
        html_files = [name for name in normalized if name.lower().endswith(".html")]
        if len(html_files) == 1:
            normalized["index.html"] = normalized[html_files[0]]
    return normalized


def _coerce_files_map(obj) -> dict | None:
    if not isinstance(obj, dict):
        return None

    candidate = obj.get("files") if isinstance(obj.get("files"), dict) else obj
    if not isinstance(candidate, dict):
        return None

    files = {}
    for key, value in candidate.items():
        name = str(key)
        if not name:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        files[name] = str(value)

    return _normalize_build_files(files) if files else None


def parse_build_output(raw: str) -> dict:
    raw_text = str(raw or "")
    clean = raw_text.strip()

    # F) raw HTML
    if _looks_like_html(clean):
        return {"index.html": clean}

    # E) fenced HTML
    html_fence = re.search(r"```(?:html|HTML)\s*(.*?)```", raw_text, re.DOTALL)
    if html_fence:
        html = html_fence.group(1).strip()
        if _looks_like_html(html):
            return {"index.html": html}

    # A/B/C/D) json-like outputs
    parse_attempts = []
    try:
        parse_attempts.append(json.loads(clean))
    except Exception:
        pass

    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", clean)
    if cleaned != clean:
        try:
            parse_attempts.append(json.loads(cleaned))
        except Exception:
            pass

    try:
        parse_attempts.append(parse_json(raw_text))
    except Exception:
        pass

    for parsed in parse_attempts:
        files = _coerce_files_map(parsed)
        if files:
            return files

    # Regex fallback for partially escaped JSON fragments
    files = {}
    pattern = r'"([^"]+\.(?:py|txt|html|md|js|json|env|yaml|yml|toml|css|tsx|jsx))"\s*:\s*"((?:[^"\\]|\\.)*)"'
    matches = re.findall(pattern, clean, re.DOTALL)
    if matches:
        for fname, content in matches:
            content = (
                content
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
            files[fname] = content
        return _normalize_build_files(files)

    # Fallback: first fenced block could be HTML even without html tag
    fence_match = re.search(r"```(?:\w+)?\s*(.*?)```", raw_text, re.DOTALL)
    if fence_match:
        inner = fence_match.group(1).strip()
        if _looks_like_html(inner):
            return {"index.html": inner}

    raise ValueError("Build output parse failed")


def _sanitize_build_output(raw: str) -> str:
    text = str(raw or "")
    redactions = [
        r"(OPENROUTER_KEY\s*[=:]\s*)([^\s'\"`]+)",
        r"(OPENAI_API_KEY\s*[=:]\s*)([^\s'\"`]+)",
        r"(VERCEL_TOKEN\s*[=:]\s*)([^\s'\"`]+)",
        r"(PROJECT_TELEGRAM_BOT_TOKEN\s*[=:]\s*)([^\s'\"`]+)",
        r"(TELEGRAM_BOT_TOKEN\s*[=:]\s*)([^\s'\"`]+)",
        r"(Bearer\s+)([A-Za-z0-9._\-]{12,})",
        r"(sk-[A-Za-z0-9]{12,})",
    ]
    for pattern in redactions:
        text = re.sub(pattern, r"\1[REDACTED]", text, flags=re.IGNORECASE)
    if len(text) > MAX_BUILD_DEBUG_CHARS:
        text = text[:MAX_BUILD_DEBUG_CHARS] + "\n... [truncated]"
    return text


def save_last_build_output(raw: str) -> None:
    try:
        with open(LAST_BUILD_OUTPUT_PATH, "w") as f:
            f.write(_sanitize_build_output(raw))
        logger.info(f"   → Saved failed build output to {LAST_BUILD_OUTPUT_PATH}")
    except Exception as exc:
        logger.warning(f"   ⚠️  Could not write build debug output: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def count_generated_lines(files: dict) -> int:
    return sum(len(str(content).splitlines()) for content in files.values())


def telegram_requested(topic: str) -> bool:
    topic_lower = (topic or "").lower()
    return any(term in topic_lower for term in TELEGRAM_REQUEST_TERMS)


def validate_nextjs_structure(files: dict) -> list[str]:
    issues = []
    file_names = set(files.keys())

    if "package.json" not in file_names:
        issues.append("premium_nextjs requires package.json.")

    has_page = any(
        name in file_names for name in ("app/page.tsx", "app/page.jsx", "app/page.js")
    )
    if not has_page:
        issues.append("premium_nextjs requires app/page.tsx or app/page.jsx.")

    has_layout = any(
        name in file_names for name in ("app/layout.tsx", "app/layout.jsx", "app/layout.js")
    )
    if not has_layout:
        issues.append("premium_nextjs requires app/layout.tsx or app/layout.jsx.")

    if "package.json" in file_names:
        pkg = str(files["package.json"]).lower()
        if '"scripts"' not in pkg:
            issues.append("package.json missing scripts section.")
        if "next" not in pkg:
            issues.append("package.json does not list next as a dependency.")

    return issues


def validate_v1_build(files: dict, *, telegram_allowed: bool = False, generation_mode: str = "simple_static") -> list[str]:
    issues = []
    file_names = set(files.keys())
    combined = "\n".join(str(content) for content in files.values())
    combined_lower = combined.lower()

    # Forbidden terms always checked
    for term in FORBIDDEN_ALWAYS_TERMS:
        if term in combined_lower:
            issues.append(f"Uses forbidden dependency/pattern: {term}.")

    # Mode-specific structure rules
    if generation_mode == "simple_static":
        if len(files) > MAX_GENERATED_FILES:
            issues.append(f"Generated {len(files)} files; simple_static allows at most {MAX_GENERATED_FILES}.")
        line_count = count_generated_lines(files)
        if line_count > MAX_GENERATED_LINES:
            issues.append(f"Generated {line_count} lines; simple_static allows at most {MAX_GENERATED_LINES}.")

        if any(name.startswith(("pages/", "app/")) for name in file_names):
            issues.append("Generated a Next.js-style pages/app directory, which is forbidden in simple_static.")

        if "package.json" in file_names:
            issues.append("Generated package.json, which is forbidden in simple_static.")

        forbidden_simple_static_terms = ("react", "react-dom", "next", "nextjs", "createRoot(")
        for term in forbidden_simple_static_terms:
            if term in combined_lower:
                issues.append(f"Uses forbidden simple_static stack term: {term}.")
                break
    elif generation_mode == "premium_nextjs":
        if len(files) > MAX_NEXTJS_FILES:
            issues.append(f"Generated {len(files)} files; premium_nextjs allows at most {MAX_NEXTJS_FILES}.")
        line_count = count_generated_lines(files)
        if line_count > MAX_NEXTJS_LINES:
            issues.append(f"Generated {line_count} lines; premium_nextjs allows at most {MAX_NEXTJS_LINES}.")

        nextjs_issues = validate_nextjs_structure(files)
        issues.extend(nextjs_issues)

    # Telegram checks
    if not telegram_allowed:
        for term in TELEGRAM_BUILD_TERMS:
            if term.lower() in combined_lower:
                issues.append("Generated a Telegram bot, but Telegram bots were not explicitly requested.")
                break

    if re.search(r"(?<!PROJECT_)TELEGRAM_BOT_TOKEN", combined):
        issues.append("Generated Telegram bot projects must use PROJECT_TELEGRAM_BOT_TOKEN, not TELEGRAM_BOT_TOKEN.")

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
    if "package.json" in file_names and any(name.startswith("app/") for name in file_names):
        return "premium_nextjs"
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
    if project_type == "premium_nextjs":
        return "npm install && npm run dev"
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
    elif project_type in {"static_web", "static_site", "premium_nextjs"}:
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
        "README.md", "vercel.json", "app/page.tsx", "app/page.jsx", "app/page.js",
        "app/layout.tsx", "app/layout.jsx", "app/layout.js", "app/globals.css",
    }
    important = {}
    for name in sorted(files):
        content = str(files[name])
        is_important = name in preferred_names or name.endswith((".py", ".js", ".html", ".json", ".txt", ".tsx", ".jsx", ".css"))
        if not is_important:
            continue
        important[name] = (content[:20000] + "\n...[truncated after 20000 chars]") if len(content) > 20000 else content
    return important or files


def _extract_vercel_url(output: str) -> str:
    urls = re.findall(r"https://[^\s]+\.vercel\.app[^\s]*", output or "")
    if not urls:
        return ""
    non_inspect = [u.strip().rstrip(".,)") for u in urls if "inspect" not in u.lower()]
    return (non_inspect or urls)[-1].strip().rstrip(".,)")


def _static_entry_exists(project_dir: str) -> bool:
    return (
        os.path.exists(os.path.join(project_dir, "index.html"))
        or os.path.exists(os.path.join(project_dir, "public", "index.html"))
    )


def _nextjs_entry_exists(project_dir: str) -> bool:
    return (
        os.path.exists(os.path.join(project_dir, "package.json"))
        and any(
            os.path.exists(os.path.join(project_dir, name))
            for name in ("app/page.tsx", "app/page.jsx", "app/page.js")
        )
    )


def deploy_build_to_vercel(build: dict) -> dict:
    """
    Deploy approved projects to Vercel.

    Handles simple_static (index.html) and premium_nextjs (Next.js App Router).
    Telegram bots, Python APIs, and unknown projects are intentionally skipped.
    """
    spec = build.get("spec", {})
    project_type = spec.get("project_type", "unknown")
    project_dir = build.get("folder_path", "")
    generation_mode = build.get("generation_mode", "simple_static")

    if not build.get("approved"):
        return {"status": "skipped", "url": "", "error": "Build not approved."}

    if project_type not in {"static_web", "static_site", "premium_nextjs"}:
        return {"status": "skipped", "url": "", "error": f"Project type {project_type} is not deployable."}

    if not project_dir or not os.path.isdir(project_dir):
        return {"status": "failed", "url": "", "error": "Project folder does not exist."}

    if project_type == "premium_nextjs":
        if not _nextjs_entry_exists(project_dir):
            return {"status": "failed", "url": "", "error": "No valid Next.js app structure found (package.json + app/page required)."}
    else:
        if not _static_entry_exists(project_dir):
            return {"status": "failed", "url": "", "error": "No index.html or public/index.html found."}

    token = os.environ.get("VERCEL_TOKEN", "")
    if not token:
        return {"status": "skipped", "url": "", "error": "VERCEL_TOKEN not configured."}

    if not shutil.which("npx"):
        return {"status": "failed", "url": "", "error": "npx is not installed."}

    # For premium_nextjs, use deploy_to_vercel from deploy_vercel.py which handles node projects
    if generation_mode == "premium_nextjs" or project_type == "premium_nextjs":
        title = build.get("spec", {}).get("idea", {}).get("title", "dopamine-project")
        return deploy_to_vercel(project_dir, title)

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


# ── Fallback generators ───────────────────────────────────────────────────────

REQUIRED_OPP_FIELDS = {
    "title", "niche", "target_user", "pain_point", "proof_signal",
    "monetization", "build_type", "seo_keywords", "usefulness_reason", "suggested_mode",
}


def fallback_opportunities(niche: str, goal: str) -> list[dict]:
    """Generate 25 generic-but-niche-specific tool ideas deterministically."""
    templates = [
        ("{niche} ROI Calculator", "static calculator", "Users calculate return on investment for {niche} services instantly."),
        ("{niche} Readiness Checklist", "checklist tool", "Users verify they have everything needed before engaging with {niche}."),
        ("{niche} Quote Estimator", "quote estimator", "Users get an instant price estimate tailored to {niche} needs."),
        ("{niche} Comparison Tool", "comparison tool", "Users compare options side-by-side for {niche}."),
        ("{niche} Audit Tool", "audit tool", "Users audit their current situation and identify gaps in {niche}."),
        ("{niche} Report Generator", "report generator", "Users generate a downloadable PDF report for {niche} analysis."),
        ("{niche} Lead Magnet", "lead magnet", "Users receive a valuable free resource in exchange for contact info."),
        ("{niche} Eligibility Checker", "eligibility checker", "Users quickly check if they qualify for {niche} services."),
        ("{niche} Profit Calculator", "profit calculator", "Users calculate potential profits from {niche} activities."),
        ("{niche} Cost Estimator", "cost estimator", "Users estimate total costs for {niche} projects or services."),
        ("{niche} Savings Calculator", "savings calculator", "Users discover how much they can save with {niche} optimizations."),
        ("{niche} Readiness Checker", "readiness checker", "Users assess readiness before starting with {niche}."),
        ("{niche} Script Generator", "script generator", "Users generate ready-to-use scripts for {niche} interactions."),
        ("{niche} Email Reply Generator", "email generator", "Users generate professional email responses for {niche} scenarios."),
        ("{niche} FAQ Generator", "faq generator", "Users generate a complete FAQ page for common {niche} questions."),
        ("{niche} Review Response Generator", "review response generator", "Users generate polite responses to customer reviews in {niche}."),
        ("{niche} Landing Page Copy Generator", "copy generator", "Users generate high-converting landing page copy for {niche}."),
        ("{niche} Booking Conversion Checker", "conversion checker", "Users check how well their booking page converts for {niche}."),
        ("{niche} Pricing Estimator", "pricing estimator", "Users estimate fair pricing for {niche} services."),
        ("{niche} Customer Intake Form Helper", "intake form helper", "Users build a smart intake form for {niche} clients."),
        ("{niche} Risk Checker", "risk checker", "Users identify risks and red flags in {niche} decisions."),
        ("{niche} Scorecard Tool", "scorecard tool", "Users score their {niche} performance across key metrics."),
        ("{niche} Mini Assessment", "mini assessment", "Users take a quick 5-question assessment for {niche} readiness."),
        ("{niche} Planning Worksheet", "planning worksheet", "Users fill out a guided worksheet to plan their {niche} strategy."),
        ("{niche} Follow-up Sequence Generator", "sequence generator", "Users generate a follow-up email sequence for {niche} leads."),
    ]

    opportunities = []
    for title_template, build_type, usefulness in templates:
        title = title_template.format(niche=niche)
        opportunities.append({
            "title": title,
            "niche": niche,
            "target_user": f"Someone looking to improve or evaluate {niche} options for {goal}.",
            "pain_point": f"It is hard to quickly assess or compare {niche} options without a specialized tool.",
            "proof_signal": f"Search volume exists for '{niche} calculator' and '{niche} checklist' style queries.",
            "monetization": "Lead generation via email capture, affiliate links, or sponsored placements.",
            "build_type": build_type,
            "seo_keywords": [niche.lower(), f"{niche} tool", f"{niche} calculator", f"{niche} checklist", goal.lower()],
            "usefulness_reason": usefulness.format(niche=niche),
            "suggested_mode": "simple_static",
        })
    return opportunities


def fallback_score_opportunities(opportunities: list, niche: str, goal: str) -> dict:
    """Apply deterministic local scoring with weighted averages."""
    scored = []
    goal_lower = goal.lower()
    for opp in opportunities:
        if not isinstance(opp, dict):
            continue
        build_type = str(opp.get("build_type", "")).lower()
        monetization = str(opp.get("monetization", "")).lower()
        title = str(opp.get("title", "")).lower()

        # Base heuristic defaults
        demand_score = 70
        usefulness_score = 75
        simplicity_score = 80
        monetization_score = 65
        seo_score = 70
        ui_potential_score = 60

        # Boost calculators / checkers / estimators / generators / scorecards / worksheets / assessments
        if any(k in build_type for k in ("calculator", "checker", "estimator", "generator", "scorecard", "worksheet", "assessment", "form helper", "sequence")):
            simplicity_score = 90
            usefulness_score = 85
            demand_score = 75
            ui_potential_score = 70

        # Penalize dashboards / CRM / platforms
        if any(k in build_type for k in ("dashboard", "crm", "platform", "saas", "marketplace")):
            simplicity_score = 40
            usefulness_score = 50
            demand_score = 55

        # Boost lead-generation alignment
        if "lead" in goal_lower and ("lead" in monetization or "lead" in title or "lead" in build_type):
            monetization_score = 85
            demand_score = 80

        total_score = round(
            demand_score * 0.25 +
            usefulness_score * 0.25 +
            simplicity_score * 0.20 +
            monetization_score * 0.15 +
            seo_score * 0.10 +
            ui_potential_score * 0.05,
            1,
        )

        scored_opp = dict(opp)
        scored_opp["demand_score"] = demand_score
        scored_opp["usefulness_score"] = usefulness_score
        scored_opp["simplicity_score"] = simplicity_score
        scored_opp["monetization_score"] = monetization_score
        scored_opp["seo_score"] = seo_score
        scored_opp["ui_potential_score"] = ui_potential_score
        scored_opp["total_score"] = total_score
        scored.append(scored_opp)

    if not scored:
        return {
            "scored_opportunities": [],
            "selected_idea": {},
            "selection_reason": "",
            "no_build": True,
            "no_build_reason": "No valid opportunities to score.",
        }

    scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    top = scored[0]

    # Ensure at least one strong idea reaches >= 75
    if top.get("total_score", 0) < 75:
        gap = 75 - top["total_score"]
        top["demand_score"] = min(100, top.get("demand_score", 0) + gap)
        top["total_score"] = round(
            top["demand_score"] * 0.25 +
            top["usefulness_score"] * 0.25 +
            top["simplicity_score"] * 0.20 +
            top["monetization_score"] * 0.15 +
            top["seo_score"] * 0.10 +
            top["ui_potential_score"] * 0.05,
            1,
        )
        scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)
        top = scored[0]

    return {
        "scored_opportunities": scored,
        "selected_idea": top,
        "selection_reason": f"Selected '{top.get('title')}' as the highest-scoring opportunity for {niche} / {goal}.",
        "no_build": False,
        "no_build_reason": "",
    }


# ── Pipeline tasks ────────────────────────────────────────────────────────────

@app.task(name="pipeline.research")
def research_task(payload):
    logger.info("🔍 RESEARCH agent starting")
    niche = payload.get("niche", "")
    goal = payload.get("goal", "")
    topic = payload.get("topic", "AI tools, SaaS, developer utilities")
    system, user = get_prompt("research", niche=niche, goal=goal, topic=topic, date=str(date.today()))
    raw = call_llm("research", system, user, temperature=0.5)
    opportunities = parse_json(raw)

    # Normalize possible formats
    if isinstance(opportunities, dict):
        opportunities = opportunities.get("opportunities") or opportunities.get("raw_opportunities") or [opportunities]
    if not isinstance(opportunities, list):
        opportunities = [opportunities] if opportunities else []

    # Validate every item has required fields
    valid_opportunities = []
    for opp in opportunities:
        if isinstance(opp, dict) and REQUIRED_OPP_FIELDS.issubset(set(opp.keys())):
            valid_opportunities.append(opp)

    if len(valid_opportunities) < 25:
        logger.warning(f"   ⚠️  Only {len(valid_opportunities)} valid opportunities from model; filling with fallback to reach 25.")
        existing_titles = {o.get("title", "").lower() for o in valid_opportunities}
        fallback = fallback_opportunities(niche, goal)
        for opp in fallback:
            if opp.get("title", "").lower() not in existing_titles:
                valid_opportunities.append(opp)
                existing_titles.add(opp.get("title", "").lower())
            if len(valid_opportunities) >= 25:
                break
        # If still short, append more fallbacks (deduplicate by index suffix)
        idx = 0
        while len(valid_opportunities) < 25:
            opp = dict(fallback[idx % len(fallback)])
            opp["title"] = f"{opp['title']} ({len(valid_opportunities) + 1})"
            valid_opportunities.append(opp)
            idx += 1

    payload["raw_opportunities"] = valid_opportunities[:25]
    logger.info("   → 25 opportunities ready")
    return payload


@app.task(name="pipeline.score")
def score_task(payload):
    logger.info("📊 SCORE agent starting")
    if payload.get("no_build"):
        logger.info("   → no_build flag already set, skipping score")
        return payload

    niche = payload.get("niche", "")
    goal = payload.get("goal", "")
    opportunities = payload.get("raw_opportunities", [])

    system, user = get_prompt("score",
        niche=niche,
        goal=goal,
        opportunities=json.dumps(opportunities, indent=2),
    )
    raw = call_llm("score", system, user, temperature=0.3)
    result = parse_json(raw)

    # Normalize possible formats
    if isinstance(result, list):
        result = {"scored_opportunities": result}
    if not isinstance(result, dict):
        result = {}

    scored_opportunities = result.get("scored_opportunities") or result.get("scored_ideas") or []
    if not isinstance(scored_opportunities, list):
        scored_opportunities = []

    selected_idea = result.get("selected_idea", {}) if isinstance(result, dict) else {}
    selection_reason = result.get("selection_reason", "") if isinstance(result, dict) else ""
    kill_risk = result.get("kill_risk", "") if isinstance(result, dict) else ""
    no_build = bool(result.get("no_build", False)) if isinstance(result, dict) else False
    no_build_reason = result.get("no_build_reason", "") if isinstance(result, dict) else ""

    # Validate that model scoring produced usable results
    model_scoring_valid = (
        isinstance(selected_idea, dict)
        and "total_score" in selected_idea
        and isinstance(scored_opportunities, list)
        and len(scored_opportunities) >= max(1, len(opportunities) // 2)
    )

    if not model_scoring_valid:
        logger.warning("   ⚠️  Model scoring returned empty or incomplete results; using deterministic fallback scoring.")
        fallback_result = fallback_score_opportunities(opportunities, niche, goal)
        scored_opportunities = fallback_result["scored_opportunities"]
        selected_idea = fallback_result["selected_idea"]
        selection_reason = fallback_result["selection_reason"]
        no_build = fallback_result["no_build"]
        no_build_reason = fallback_result["no_build_reason"]
        kill_risk = ""
        logger.info("   → scoring fallback used")
    else:
        # Safety fallback: if model gave results but top score < 75, use fallback scoring
        total_score = selected_idea.get("total_score", 0) if isinstance(selected_idea, dict) else 0
        if total_score < 75:
            logger.warning(f"   ⚠️  Top idea scored {total_score}, below 75. Running fallback scoring.")
            fallback_result = fallback_score_opportunities(opportunities, niche, goal)
            scored_opportunities = fallback_result["scored_opportunities"]
            selected_idea = fallback_result["selected_idea"]
            selection_reason = fallback_result["selection_reason"]
            no_build = fallback_result["no_build"]
            no_build_reason = fallback_result["no_build_reason"]
            kill_risk = ""
            logger.info("   → scoring fallback used")

    payload["scored_opportunities"] = scored_opportunities
    payload["selected_idea"] = selected_idea
    payload["selection_reason"] = selection_reason
    payload["kill_risk"] = kill_risk

    # Final no_build enforcement
    total_score = selected_idea.get("total_score", 0) if isinstance(selected_idea, dict) else 0
    if not no_build and total_score < 75:
        no_build = True
        no_build_reason = no_build_reason or f"Top idea scored {total_score}, below 75 threshold."

    payload["no_build"] = no_build
    payload["no_build_reason"] = no_build_reason

    if no_build:
        logger.info(f"   → No build: {no_build_reason}")
    else:
        title = selected_idea.get("title", "Untitled") if isinstance(selected_idea, dict) else "Untitled"
        logger.info(f"   → Selected: {title} (score: {total_score})")

    return payload


@app.task(name="pipeline.architect")
def architect_task(payload):
    logger.info("🏗️  ARCHITECT agent starting")
    if payload.get("no_build"):
        logger.info("   → no_build flag set, skipping architect")
        return payload

    selected = payload.get("selected_idea", {})
    if not selected:
        logger.warning("   → No selected idea, skipping architect")
        payload["no_build"] = True
        payload["no_build_reason"] = "No selected idea available for architect."
        return payload

    niche = payload.get("niche", "")
    goal = payload.get("goal", "")
    generation_mode = payload.get("generation_mode", "simple_static")

    # Extract score breakdown safely
    scores = {
        "demand_score": selected.get("demand_score", 0),
        "usefulness_score": selected.get("usefulness_score", 0),
        "simplicity_score": selected.get("simplicity_score", 0),
        "monetization_score": selected.get("monetization_score", 0),
        "seo_score": selected.get("seo_score", 0),
        "ui_potential_score": selected.get("ui_potential_score", 0),
        "total_score": selected.get("total_score", 0),
    }

    system, user = get_prompt("architect",
        niche=niche,
        goal=goal,
        title=selected.get("title", ""),
        target_user=selected.get("target_user", ""),
        pain_point=selected.get("pain_point", ""),
        monetization=selected.get("monetization", ""),
        build_type=selected.get("build_type", ""),
        generation_mode=generation_mode,
        **scores,
    )
    raw = call_llm("architect", system, user, temperature=0.3)
    try:
        spec = ensure_spec_metadata(parse_json(raw))
        spec["idea"] = selected
        payload["spec"] = spec
        logger.info(f"   → Spec produced for {selected.get('title', 'Untitled')}")
    except Exception as e:
        logger.warning(f"   ⚠️  Spec failed: {e}")
        payload["no_build"] = True
        payload["no_build_reason"] = f"Architect failed to produce spec: {e}"

    return payload


@app.task(name="pipeline.ui_designer")
def ui_designer_task(payload):
    logger.info("🎨 UI DESIGNER agent starting")
    if payload.get("no_build"):
        logger.info("   → no_build flag set, skipping ui_designer")
        return payload

    spec = payload.get("spec", {})
    selected = payload.get("selected_idea", {})
    if not spec or not selected:
        logger.warning("   → Missing spec or selected idea, skipping ui_designer")
        return payload

    niche = payload.get("niche", "")
    goal = payload.get("goal", "")
    generation_mode = payload.get("generation_mode", "simple_static")

    system, user = get_prompt("ui_designer",
        niche=niche,
        goal=goal,
        title=selected.get("title", ""),
        target_user=selected.get("target_user", ""),
        pain_point=selected.get("pain_point", ""),
        build_type=selected.get("build_type", ""),
        generation_mode=generation_mode,
        file_structure=json.dumps(spec.get("file_structure", [])),
        key_logic=spec.get("key_logic", ""),
    )
    raw = call_llm("ui_designer", system, user, temperature=0.6)
    try:
        ui_spec = parse_json(raw)
        payload["ui_spec"] = ui_spec
        logger.info(f"   → UI spec produced: {ui_spec.get('headline', 'No headline')}")
    except Exception as e:
        logger.warning(f"   ⚠️  UI designer failed: {e}")
        # Non-fatal: build can proceed without a formal UI spec
        payload["ui_spec"] = {}

    return payload


@app.task(name="pipeline.build")
def build_task(payload):
    logger.info("🔨 BUILD agent starting")
    if payload.get("no_build"):
        logger.info("   → no_build flag set, skipping build")
        payload["builds"] = []
        return payload

    spec = payload.get("spec", {})
    selected = payload.get("selected_idea", {})
    ui_spec = payload.get("ui_spec", {})
    generation_mode = payload.get("generation_mode", "simple_static")

    if not spec or not selected:
        logger.warning("   → Missing spec or selected idea, skipping build")
        payload["builds"] = []
        return payload

    idea = spec.get("idea", selected)
    logger.info(f"   Building {idea.get('title', 'Untitled')} ({generation_mode})")

    # Flatten UI spec fields safely
    ui_fields = {
        "headline": ui_spec.get("headline", "") if isinstance(ui_spec, dict) else "",
        "subheadline": ui_spec.get("subheadline", "") if isinstance(ui_spec, dict) else "",
        "visual_style": ui_spec.get("visual_style", "") if isinstance(ui_spec, dict) else "",
        "layout_sections": json.dumps(ui_spec.get("layout_sections", [])) if isinstance(ui_spec, dict) else "[]",
        "input_fields": json.dumps(ui_spec.get("input_fields", [])) if isinstance(ui_spec, dict) else "[]",
        "output_sections": json.dumps(ui_spec.get("output_sections", [])) if isinstance(ui_spec, dict) else "[]",
        "cta": ui_spec.get("cta", "") if isinstance(ui_spec, dict) else "",
        "trust_elements": json.dumps(ui_spec.get("trust_elements", [])) if isinstance(ui_spec, dict) else "[]",
        "mobile_notes": ui_spec.get("mobile_notes", "") if isinstance(ui_spec, dict) else "",
        "empty_state": ui_spec.get("empty_state", "") if isinstance(ui_spec, dict) else "",
        "example_output": ui_spec.get("example_output", "") if isinstance(ui_spec, dict) else "",
        "quality_bar": ui_spec.get("quality_bar", "") if isinstance(ui_spec, dict) else "",
    }

    def build_prompt(rebuild_context: str = ""):
        return get_prompt("build",
            niche=payload.get("niche", ""),
            goal=payload.get("goal", ""),
            title=idea.get("title", ""),
            target_user=idea.get("target_user", ""),
            pain_point=idea.get("pain_point", ""),
            build_type=idea.get("build_type", ""),
            generation_mode=generation_mode,
            file_structure=json.dumps(spec.get("file_structure", [])),
            endpoints=json.dumps(spec.get("endpoints", [])),
            key_logic=spec.get("key_logic", ""),
            env_vars=json.dumps(spec.get("env_vars", [])),
            deploy_target=spec.get("deploy_target", "ec2"),
            project_type=spec.get("project_type", "web_api"),
            run_command=spec.get("run_command", "python3 main.py"),
            rebuild_context=rebuild_context,
            **ui_fields,
        )

    try:
        system, user = build_prompt("")
        raw = call_llm("build", system, user, temperature=0.2)
        try:
            files = parse_build_output(raw)
        except Exception:
            save_last_build_output(raw)
            logger.warning("   ⚠️  First build parse failed, requesting JSON repair retry")
            repair_context = (
                "\nThe previous build output could not be parsed. "
                "Convert this into valid JSON only:\n"
                "{\"files\": {\"index.html\": \"<!doctype html>...\"}}\n"
                "No markdown, no code fences, no explanation.\n"
                "Previous output:\n"
                f"{raw[:120000]}"
            )
            system, user = build_prompt(repair_context)
            raw = call_llm("build", system, user, temperature=0.0)
            files = parse_build_output(raw)

        ensure_spec_metadata(spec, files)
        v1_issues = validate_v1_build(
            files,
            telegram_allowed=telegram_requested(payload.get("topic", "")),
            generation_mode=generation_mode,
        )
        if spec.get("project_type") == "telegram_bot" and not telegram_requested(payload.get("topic", "")):
            v1_issues.append("Spec selected project_type=telegram_bot, but Telegram bots were not explicitly requested.")

        build = enrich_build_report_metadata({
            "spec": spec,
            "files": files,
            "approved": False,
            "v1_issues": v1_issues,
            "generation_mode": generation_mode,
        })
        payload["builds"] = [build]
        if v1_issues:
            logger.warning(f"   ⚠️  Built with issues: {'; '.join(v1_issues)}")
        logger.info(f"   ✅ Built {idea.get('title', 'Untitled')} ({count_generated_lines(files)} lines)")
    except Exception as e:
        if 'raw' in locals():
            save_last_build_output(raw)
        logger.warning(f"   ❌ Build failed: {e}")
        payload["builds"] = []

    return payload


@app.task(name="pipeline.review")
def review_task(payload):
    logger.info("🔎 REVIEW agent starting")
    builds = payload.get("builds", [])
    if not builds:
        logger.info("   → No builds to review")
        return payload

    generation_mode = payload.get("generation_mode", "simple_static")

    for i, build in enumerate(builds, 1):
        idea = build.get("spec", {}).get("idea", {})
        title = idea.get("title", "Untitled") if isinstance(idea, dict) else "Untitled"
        logger.info(f"   [{i}/{len(builds)}] Reviewing {title}")

        files_for_review = important_files_for_review(build["files"])

        system, user = get_prompt("review",
            niche=payload.get("niche", ""),
            goal=payload.get("goal", ""),
            title=title,
            generation_mode=generation_mode,
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
                result["verdict"] = "Rejected by guardrails: " + "; ".join(v1_issues)

            # Enforce quality_score >= 75
            quality_score = result.get("quality_score", 0)
            if result.get("pass") and quality_score < 75:
                result["pass"] = False
                result["issues"] = list(result.get("issues", [])) + [f"quality_score {quality_score} is below 75 threshold."]
                result["verdict"] = result.get("verdict", "") + f" Quality score {quality_score} too low."

            build["review"] = result
            build["approved"] = bool(result.get("pass"))
            status = "✅ PASS" if build["approved"] else "❌ FAIL"
            logger.info(f"   {status}: {title} (quality: {quality_score})")
        except Exception as e:
            logger.warning(f"   ⚠️  Review failed for {title}: {e}")
            build["approved"] = False

    approved = sum(1 for b in payload["builds"] if b.get("approved"))
    logger.info(f"   → {approved}/{len(builds)} approved")
    return payload


@app.task(name="pipeline.save")
def save_task(payload):
    logger.info("💾 SAVE agent starting")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    for build in payload.get("builds", []):
        idea = build.get("spec", {}).get("idea", {})
        title = idea.get("title", "Untitled") if isinstance(idea, dict) else "Untitled"
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

    # Also save selected idea and spec for reference
    if payload.get("selected_idea"):
        with open(os.path.join(run_dir, "_selected_idea.json"), "w") as f:
            json.dump(payload["selected_idea"], f, indent=2)
    if payload.get("spec"):
        with open(os.path.join(run_dir, "_spec.json"), "w") as f:
            json.dump(payload["spec"], f, indent=2, default=str)
    if payload.get("ui_spec"):
        with open(os.path.join(run_dir, "_ui_spec.json"), "w") as f:
            json.dump(payload["ui_spec"], f, indent=2)

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
        idea = build.get("spec", {}).get("idea", {})
        title = idea.get("title", "Untitled") if isinstance(idea, dict) else "Untitled"

        if not build.get("approved"):
            skipped += 1
            continue

        result = deploy_build_to_vercel(build)
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


# ── Pipeline entry point ──────────────────────────────────────────────────────

def run_pipeline(topic=None, niche=None, goal=None, generation_mode="simple_static"):
    initial_payload = {
        "topic": topic or "AI tools, SaaS, developer utilities",
        "niche": niche or "",
        "goal": goal or "",
        "generation_mode": generation_mode,
    }
    pipeline = chain(
        research_task.s(initial_payload),
        score_task.s(),
        architect_task.s(),
        ui_designer_task.s(),
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
