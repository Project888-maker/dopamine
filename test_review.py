"""
test_review.py
Feeds the actual built code into review agent and checks verdict.
"""

import os
import json
import time
from openai import OpenAI
from prompts import get_prompt

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
MODEL = "anthropic/claude-haiku-4.5"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

def load_file(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except:
        return ""

FILES = {
    "main.py":           load_file("/home/ubuntu/pipeline/test_output/main.py"),
    "requirements.txt":  load_file("/home/ubuntu/pipeline/test_output/requirements.txt"),
    "static/index.html": load_file("/home/ubuntu/pipeline/test_output/static_index.html"),
    "README.md":         load_file("/home/ubuntu/pipeline/test_output/README.md"),
}

def test_review_agent():
    print("\n" + "="*60)
    print("🔎 Testing Review Agent")
    print(f"   Model  : {MODEL}")
    print(f"   Product: Synthetic CSV Maker")
    print("="*60)

    # Send main.py in full, truncate others
    files_preview = {}
    for k, v in FILES.items():
        if k == "main.py":
            files_preview[k] = v  # full file
        else:
            files_preview[k] = v[:500]

    system, user = get_prompt("review",
        title="Synthetic CSV Maker",
        stack="FastAPI + Pydantic + GPT-4 + Faker",
        files_preview=json.dumps(files_preview, indent=2),
    )
    print("\n✅ Step 1: Prompts loaded")

    print("⏳ Step 2: Calling OpenRouter...")
    start = time.time()

    try:
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0.1,
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
        result = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"❌ Step 3 FAILED — Invalid JSON: {e}")
        print("\n--- RAW OUTPUT ---")
        print(raw[:800])
        return False

    print("✅ Step 3: Valid JSON")

    print("⏳ Step 4: Validating structure...")
    required_keys = {"pass", "issues", "verdict", "confidence"}
    missing = required_keys - set(result.keys())
    if missing:
        print(f"❌ Step 4 FAILED — Missing keys: {missing}")
        return False

    print(f"✅ Step 4: All keys present")

    print("\n" + "="*60)
    print("📋 REVIEW OUTPUT:")
    print("="*60)
    print(f"\n  Verdict    : {result['verdict']}")
    print(f"  Pass       : {result['pass']}")
    print(f"  Confidence : {result['confidence']}")
    if result["issues"]:
        print(f"\n  Issues found:")
        for issue in result["issues"]:
            print(f"    ⚠️  {issue}")
    else:
        print(f"\n  No issues found ✅")

    status = "✅ PASS" if result["pass"] else "❌ FAIL"
    print(f"\n  Final decision: {status}")

    print("\n" + "="*60)
    print("✅ ALL CHECKS PASSED — Review agent is working")
    print("="*60 + "\n")
    return True

if __name__ == "__main__":
    success = test_review_agent()
    exit(0 if success else 1)
