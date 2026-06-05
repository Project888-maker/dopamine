"""
test_architect.py
Feeds one idea into architect agent and validates the spec.
"""

import os
import json
import time
from openai import OpenAI
from prompts import get_prompt

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
MODEL = "anthropic/claude-sonnet-4-5"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

# Using idea #10 from brainstorm — simplest one, good test case
IDEA = {
    "title": "Synthetic CSV Maker",
    "description": "Describe your schema in one sentence, receive 10k rows of realistic test data as CSV or SQL dump.",
    "stack": "FastAPI + Pydantic + GPT-4 + Faker",
    "why_now": "Developers constantly need realistic test data but existing tools are clunky or expensive.",
    "monetisation": "100 rows free, $7 per 10k-row download.",
    "complexity": "low"
}

def test_architect_agent():
    print("\n" + "="*60)
    print("🏗️  Testing Architect Agent")
    print(f"   Model  : {MODEL}")
    print(f"   Product: {IDEA['title']}")
    print("="*60)

    system, user = get_prompt("architect",
        title=IDEA["title"],
        description=IDEA["description"],
        stack=IDEA["stack"],
        why_now=IDEA["why_now"],
        monetisation=IDEA["monetisation"],
    )
    print("\n✅ Step 1: Prompts loaded")

    print("⏳ Step 2: Calling OpenRouter...")
    start = time.time()

    try:
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0.3,
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
    clean = raw.replace("```json", "").replace("```", "").strip()

    try:
        spec = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"❌ Step 3 FAILED — Invalid JSON: {e}")
        print("\n--- RAW OUTPUT ---")
        print(raw[:800])
        return False

    print("✅ Step 3: Valid JSON")

    print("⏳ Step 4: Validating structure...")
    required_keys = {"file_structure", "endpoints", "key_logic", "env_vars", "deploy_target", "estimated_lines"}
    missing = required_keys - set(spec.keys())
    if missing:
        print(f"❌ Step 4 FAILED — Missing keys: {missing}")
        return False

    if spec["estimated_lines"] > 250:
        print(f"⚠️  WARNING — Estimated {spec['estimated_lines']} lines, may be over budget")
    else:
        print(f"✅ Step 4: Estimated lines: {spec['estimated_lines']} (within budget)")

    if spec["deploy_target"] not in ("vercel", "ec2", "static"):
        print(f"❌ Step 4 FAILED — Invalid deploy_target: {spec['deploy_target']}")
        return False

    print(f"✅ Step 4: All keys present, deploy target: {spec['deploy_target']}")

    print("\n" + "="*60)
    print("📋 ARCHITECT OUTPUT:")
    print("="*60)
    print(f"\n  Files:")
    for f in spec["file_structure"]:
        print(f"    - {f}")
    print(f"\n  Endpoints:")
    for e in spec["endpoints"]:
        print(f"    - {e}")
    print(f"\n  Core logic:\n    {spec['key_logic']}")
    print(f"\n  Env vars: {spec['env_vars']}")
    print(f"  Deploy: {spec['deploy_target']}")
    print(f"  Est. lines: {spec['estimated_lines']}")

    print("\n" + "="*60)
    print("✅ ALL CHECKS PASSED — Architect agent is working")
    print("="*60 + "\n")
    return True

if __name__ == "__main__":
    success = test_architect_agent()
    exit(0 if success else 1)
