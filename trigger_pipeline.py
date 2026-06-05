"""
Manual entry point for the Dopamine Celery pipeline.

Usage:
    python trigger_pipeline.py "AI tools, SaaS, developer utilities"
"""

import sys

from tasks import run_pipeline


def main() -> int:
    topic = sys.argv[1] if len(sys.argv) > 1 else "AI tools, SaaS, developer utilities"
    result = run_pipeline(topic)
    print(f"Queued Dopamine pipeline for topic: {topic}")
    print(f"Celery task id: {result.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
