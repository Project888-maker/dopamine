"""
Manual entry point for the Dopamine Celery pipeline.

Usage:
    # New niche + goal workflow
    python trigger_pipeline.py "dental clinics" "patient lead generation"
    python trigger_pipeline.py "Amazon sellers" "profit calculator" --mode premium_nextjs

    # Backward compatible single-topic workflow
    python trigger_pipeline.py "simple static HTML tools for founders"
"""

import sys

from tasks import run_pipeline


def parse_args(args: list[str]) -> dict:
    """Parse CLI args into niche, goal, topic, and generation_mode."""
    mode = "simple_static"
    # Extract --mode flag if present
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        else:
            filtered.append(args[i])
            i += 1

    if len(filtered) == 0:
        return {
            "topic": "AI tools, SaaS, developer utilities",
            "niche": "",
            "goal": "",
            "generation_mode": mode,
        }

    if len(filtered) == 1:
        # Backward compatible: single topic string
        topic = filtered[0]
        return {
            "topic": topic,
            "niche": "",
            "goal": "",
            "generation_mode": mode,
        }

    # New workflow: niche + goal
    niche = filtered[0]
    goal = filtered[1]
    topic = f"{niche} {goal}"
    return {
        "topic": topic,
        "niche": niche,
        "goal": goal,
        "generation_mode": mode,
    }


def main() -> int:
    args = sys.argv[1:]
    parsed = parse_args(args)
    result = run_pipeline(
        topic=parsed["topic"],
        niche=parsed["niche"],
        goal=parsed["goal"],
        generation_mode=parsed["generation_mode"],
    )
    print(f"Queued Dopamine pipeline for topic: {parsed['topic']}")
    if parsed["niche"]:
        print(f"Niche: {parsed['niche']}")
        print(f"Goal: {parsed['goal']}")
    print(f"Mode: {parsed['generation_mode']}")
    print(f"Celery task id: {result.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
