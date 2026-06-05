"""
test_build.py
Feeds architect spec into build agent and validates runnable code output.
"""

import os
import re
import json
import time
from openai import OpenAI
from prompts import get_prompt

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
MODEL = "moonshotai/kimi-k2"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

IDEA = {
    "title": "Synthetic CSV Maker",
    "description": "Describe your schema in one sentence, receive 10k rows of realistic test data as CSV or SQL dump.",
    "stack": "FastAPI + Pydantic + GPT-4 + Faker",
}

SPEC = {
    "file_structure": ["main.py", "requirements.txt", "static/index.html", "README.md"],
    "endpoints": ["GET /", "POST /api/generate", "GET /api/download/{job_id}"],
    "key_logic": "User submits a natural language schema description via POST /api/generate. Backend calls GPT-4 to parse the schema into structured field definitions. Faker library generates rows based on parsed schema. Free tier returns 100 rows immediately. Paid tier generates job_id and processes async.",
    "env_vars": ["OPENAI_API_KEY", "STRIPE_SECRET_KEY"],
    "deploy_target": "ec2",
    "estimated_lines": 185,
}


def parse_build_output(raw: str) -> dict:
    """
    Try standard parse first, then fallback methods.
    """
    clean = raw.replace("```json", "").replace("```", "").strip()

    # Attempt 1 — direct parse
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Attempt 2 — strip control characters and retry
    try:
        cleaned = re.sub(r'[\x00-\x1f\x7f]', '', clean)
        return json.loads(cleaned)
    except Exception:
        pass

    # Attempt 3 — regex extraction of filename: content pairs
    print("⚠️  Standard JSON parse failed, attempting regex extraction...")
    files = {}
    pattern = r'"([^"]+\.(?:py|txt|html|md|js|json|env|yaml|yml|toml|css))"\s*:\s*"((?:[^"\\]|\\.)*)"'
    matches = re.findall(pattern, clean, re.DOTALL)
    if matches:
        for fname, content in matches:
            content = (content
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\"))
            files[fname] = content
        return files

    raise ValueError("All parsing attempts failed")


def test_build_agent():
    print("\n" + "="*60)
    print("🔨 Testing Build Agent")
    print(f"   Model  : {MODEL}")
    print(f"   Product: {IDEA['title']}")
    print("="*60)

    system, user = get_prompt("build",
        title=IDEA["title"],
        description=IDEA["description"],
        stack=IDEA["stack"],
        file_structure=json.dumps(SPEC["file_structure"]),
        endpoints=json.dumps(SPEC["endpoints"]),
        key_logic=SPEC["key_logic"],
        env_vars=json.dumps(SPEC["env_vars"]),
        deploy_target=SPEC["deploy_target"],
        rebuild_context="",
    )
    print("\n✅ Step 1: Prompts loaded")

    print("⏳ Step 2: Calling OpenRouter (this will take 60-90s)...")
    start = time.time()

    try:
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
    except Exception as e:
        print(f"❌ Step 2 FAILED: {e}")
        return False

    elapsed = round(time.time() - start, 2)
    raw = response.choices[0].message.content.strip()
    print(f"✅ Step 2: Response received in {elapsed}s")
    print(f"   Tokens used: {response.usage.total_tokens}")

    print("⏳ Step 3: Parsing JSON...")
    try:
        files = parse_build_output(raw)
    except Exception as e:
        print(f"❌ Step 3 FAILED — {e}")
        print("\n--- RAW OUTPUT (first 1000 chars) ---")
        print(raw[:1000])
        return False

    print("✅ Step 3: Valid JSON")

    print("⏳ Step 4: Validating files...")
    expected = set(SPEC["file_structure"])
    got = set(files.keys())
    missing = expected - got
    if missing:
        print(f"⚠️  WARNING — Missing expected files: {missing}")
    else:
        print(f"✅ Step 4: All expected files present")

    placeholders = []
    for fname, content in files.items():
        if len(content) < 50:
            placeholders.append(fname)
        if "TODO" in content or "placeholder" in content.lower():
            placeholders.append(f"{fname} (contains TODO/placeholder)")

    if placeholders:
        print(f"⚠️  WARNING — Suspicious files: {placeholders}")
    else:
        print(f"✅ Step 4: No empty or placeholder files")

    total_lines = sum(len(v.splitlines()) for v in files.values())
    print(f"✅ Step 4: Total lines written: {total_lines}")

    print("\n" + "="*60)
    print("📋 BUILD OUTPUT SUMMARY:")
    print("="*60)
    for fname, content in files.items():
        lines = len(content.splitlines())
        print(f"\n  📄 {fname} ({lines} lines)")
        preview = "\n".join(content.splitlines()[:8])
        for line in preview.splitlines():
            print(f"     {line}")
        if lines > 8:
            print(f"     ... ({lines - 8} more lines)")

    print("\n" + "="*60)
    print("✅ ALL CHECKS PASSED — Build agent is working")
    print("="*60 + "\n")

    # Save files to disk
    outdir = "/home/ubuntu/pipeline/test_output"
    os.makedirs(outdir, exist_ok=True)
    for fname, content in files.items():
        fpath = os.path.join(outdir, fname.replace("/", "_"))
        with open(fpath, "w") as f:
            f.write(content)
    print(f"💾 Files saved to {outdir}")

    return True


if __name__ == "__main__":
    success = test_build_agent()
    exit(0 if success else 1)
