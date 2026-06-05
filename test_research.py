"""
test_research.py
Run this on EC2 to validate the research agent before firing the full chain.

Usage:
    python test_research.py

Checks:
    1. OpenRouter connection works
    2. Model responds
    3. Output is valid JSON
    4. Output is a list of 25 strings
"""

import os
import json
import time
from datetime import date
from openai import OpenAI
from prompts import get_prompt

# ── Config ────────────────────────────────────────────────────────────────────

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
MODEL = "google/gemini-2.5-flash"
TOPIC = "AI tools, SaaS micro-products, developer utilities"

if not OPENROUTER_KEY:
    print("❌ OPENROUTER_KEY not set. Run: export OPENROUTER_KEY=your_key")
    exit(1)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)


# ── Test ──────────────────────────────────────────────────────────────────────

def test_research_agent():
    print("\n" + "="*60)
    print("🔍 Testing Research Agent")
    print(f"   Model : {MODEL}")
    print(f"   Topic : {TOPIC}")
    print("="*60)

    # Step 1 — build prompts
    system, user = get_prompt(
        "research",
        topic=TOPIC,
        date=str(date.today())
    )
    print("\n✅ Step 1: Prompts loaded")

    # Step 2 — call OpenRouter
    print("⏳ Step 2: Calling OpenRouter...")
    start = time.time()

    try:
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0.5,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
    except Exception as e:
        print(f"❌ Step 2 FAILED — API call error: {e}")
        return False

    elapsed = round(time.time() - start, 2)
    raw = response.choices[0].message.content.strip()
    print(f"✅ Step 2: Response received in {elapsed}s")
    print(f"   Tokens used: {response.usage.total_tokens}")

    # Step 3 — parse JSON
    print("⏳ Step 3: Parsing JSON...")
    clean = raw.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"❌ Step 3 FAILED — Invalid JSON: {e}")
        print("\n--- RAW OUTPUT ---")
        print(raw[:500])
        return False

    print("✅ Step 3: Valid JSON")

    # Step 4 — validate structure
    print("⏳ Step 4: Validating structure...")

    if not isinstance(parsed, list):
        print(f"❌ Step 4 FAILED — Expected list, got {type(parsed)}")
        return False

    if len(parsed) < 20:
        print(f"⚠️  Step 4 WARNING — Only {len(parsed)} trends (expected ~25)")
    else:
        print(f"✅ Step 4: Got {len(parsed)} trends")

    non_strings = [i for i, x in enumerate(parsed) if not isinstance(x, str)]
    if non_strings:
        print(f"❌ Step 4 FAILED — Non-string items at indexes: {non_strings}")
        return False

    print("✅ Step 4: All items are strings")

    # Step 5 — print results
    print("\n" + "="*60)
    print("📋 RESEARCH OUTPUT:")
    print("="*60)
    for i, trend in enumerate(parsed, 1):
        print(f"  {i:02d}. {trend}")

    print("\n" + "="*60)
    print("✅ ALL CHECKS PASSED — Research agent is working")
    print("="*60 + "\n")
    return True


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    success = test_research_agent()
    exit(0 if success else 1)
