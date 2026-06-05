"""
deploy_vercel.py
Deploys a built project to Vercel and returns the live URL.
"""

import os
import re
import json
import shutil
import subprocess
import tempfile
import logging

logger = logging.getLogger(__name__)

VERCEL_TOKEN = os.environ.get("VERCEL_TOKEN", "")


def detect_project_type(project_dir: str) -> str:
    files = os.listdir(project_dir)
    if "package.json" in files:
        return "node"
    if "requirements.txt" in files or "main.py" in files or "app.py" in files:
        return "python"
    if "index.html" in files:
        return "static"
    return "static"


def write_vercel_config(project_dir: str, project_type: str):
    config = {"version": 2}

    if project_type == "python":
        entry = "main.py"
        for candidate in ["main.py", "app.py", "server.py", "api.py"]:
            if os.path.exists(os.path.join(project_dir, candidate)):
                entry = candidate
                break

        config["builds"] = [
            {"src": entry, "use": "@vercel/python"},
        ]
        if os.path.exists(os.path.join(project_dir, "static")):
            config["builds"].append({"src": "static/**", "use": "@vercel/static"})
        config["routes"] = [
            {"src": "/static/(.*)", "dest": "/static/$1"},
            {"src": "/(.*)", "dest": f"/{entry}"},
        ]

    config_path = os.path.join(project_dir, "vercel.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def deploy_to_vercel(project_dir: str, project_name: str) -> dict:
    if not VERCEL_TOKEN:
        return {"status": "failed", "url": "", "logs": "", "error": "VERCEL_TOKEN not set"}

    if not os.path.exists(project_dir):
        return {"status": "failed", "url": "", "logs": "", "error": f"Project dir not found: {project_dir}"}

    safe_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower())[:50].strip('-')
    if not safe_name:
        safe_name = "dopamine-project"

    project_type = detect_project_type(project_dir)
    write_vercel_config(project_dir, project_type)

    logger.info(f"🚀 Deploying {safe_name} ({project_type}) to Vercel")

    try:
        result = subprocess.run(
            ["vercel", "--token", VERCEL_TOKEN, "--yes", "--name", safe_name, "--prod"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )

        logs = result.stdout + "\n" + result.stderr

        if result.returncode != 0:
            return {"status": "failed", "url": "", "logs": logs[-2000:], "error": f"Vercel CLI exited {result.returncode}"}

        url_match = re.search(r'https://[a-zA-Z0-9.-]+\.vercel\.app', logs)
        if url_match:
            url = url_match.group(0)
            logger.info(f"✅ Deployed: {url}")
            return {"status": "deployed", "url": url, "logs": logs[-1000:], "error": None}

        return {"status": "failed", "url": "", "logs": logs[-2000:], "error": "Could not parse Vercel URL"}

    except subprocess.TimeoutExpired:
        return {"status": "failed", "url": "", "logs": "", "error": "Vercel deploy timed out after 180s"}
    except Exception as e:
        return {"status": "failed", "url": "", "logs": "", "error": str(e)}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python deploy_vercel.py <project_dir> [project_name]")
        sys.exit(1)
    project_dir = sys.argv[1]
    project_name = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(project_dir)
    result = deploy_to_vercel(project_dir, project_name)
    print(json.dumps(result, indent=2))
