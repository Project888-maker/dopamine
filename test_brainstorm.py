"""
test_brainstorm.py
Feeds research output directly into brainstorm agent.
"""

import os
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

# Use real output from your research test
TRENDS = [
    "AI-powered cold email personalization assistants",
    "No-code internal tool builders with AI data connectors",
    "SaaS for generating hyper-realistic AI voiceovers for marketing",
    "AI agents for automated customer support triage and response drafting",
    "Micro-SaaS for converting long-form content into social media carousels",
    "Developer tools for real-time AI model performance monitoring",
    "AI-driven content repurposing platforms for podcasts/videos to blogs",
    "Subscription boxes for curated AI prompt engineering templates",
    "AI-powered website accessibility checkers and fixers",
    "Micro-SaaS for generating personalized video greetings at scale",
    "AI tools for automated code documentation and explanation",
    "SaaS for creating interactive AI-driven product demos",
    "AI-powered tools for generating marketing copy variations for A/B testing",
    "Platforms for creating and selling custom GPTs for niche tasks",
    "AI-driven tools for summarizing lengthy legal or technical documents",
    "Micro-SaaS for generating unique AI art for blog posts/social media",
    "AI-powered tools for optimizing cloud spend by identifying unused resources",
    "SaaS for creating personalized learning paths using AI adaptation",
    "AI-driven tools for generating synthetic data for testing and development",
    "Micro-SaaS for automated meeting minute generation and action item extraction",
    "AI-powered tools for real-time sentiment analysis of customer feedback",
    "SaaS for generating personalized sales outreach scripts based on CRM data",
    "AI-driven tools for detecting and correcting grammatical errors in code comments",
    "Micro-SaaS for creating interactive quizzes and surveys with AI feedback",
    "AI-powered tools for transcribing and summarizing video calls into actionable insights",
]

def test_brainstorm_agent():
    print("\n" + "="*60)
    print("💡 Testing Brainstorm Agent")
    print(f"   Model : {MODEL}")
    print("="*60)

    system, user = get_prompt("brainstorm", trends=json.dumps(TRENDS, indent=2))
    print("\n✅ Step 1: Prompts loaded")

    print("⏳ Step 2: Calling OpenRouter...")
    start = time.time()

    try:
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0.8,
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
        parsed = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"❌ Step 3 FAILED — Invalid JSON: {e}")
        print("\n--- RAW OUTPUT ---")
        print(raw[:800])
        return False

    print("✅ Step 3: Valid JSON")

    print("⏳ Step 4: Validating structure...")
    required_keys = {"title", "description", "stack", "why_now", "monetisation", "complexity"}

    for i, idea in enumerate(parsed):
        missing = required_keys - set(idea.keys())
        if missing:
            print(f"❌ Step 4 FAILED — Idea {i+1} missing keys: {missing}")
            return False

    print(f"✅ Step 4: Got {len(parsed)} ideas, all keys present")

    print("\n" + "="*60)
    print("📋 BRAINSTORM OUTPUT:")
    print("="*60)
    for i, idea in enumerate(parsed, 1):
        print(f"\n  {i:02d}. {idea['title']} [{idea['complexity']}]")
        print(f"      {idea['description']}")
        print(f"      Stack: {idea['stack']}")
        print(f"      Money: {idea['monetisation']}")

    print("\n" + "="*60)
    print("✅ ALL CHECKS PASSED — Brainstorm agent is working")
    print("="*60 + "\n")
    return True

if __name__ == "__main__":
    success = test_brainstorm_agent()
    exit(0 if success else 1)
