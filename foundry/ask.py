"""Ask the Arrivo Foundry agent a question from the command line.

Uses the v2 Foundry agents. Set FOUNDRY_USE_TEAM=1 in backend/.env for the
orchestrated multi-agent team instead of the single grounded agent.

Usage:
    python foundry/ask.py "When must I convert my overseas driver licence in NSW?"
    python foundry/ask.py            # then type questions interactively, Ctrl-D to quit
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV_PATH = Path(__file__).resolve().parent.parent / "backend" / ".env"


def build_agent():
    """Single grounded agent, or the orchestrated team when FOUNDRY_USE_TEAM=1."""
    use_team = os.environ.get("FOUNDRY_USE_TEAM", "").lower() in ("1", "true", "yes")
    if use_team and os.environ.get("FOUNDRY_V2_ORCHESTRATOR_NAME"):
        from foundry.v2 import FoundryV2Team
        return FoundryV2Team()
    from foundry.v2 import FoundryV2Agent
    return FoundryV2Agent()


def _has_evaluator(agent) -> bool:
    return bool(
        getattr(agent, "evaluator_name", None)
        or getattr(getattr(agent, "base", None), "evaluator_name", None)
    )


def show(agent, question: str) -> None:
    print(f"\nQ: {question}")
    if _has_evaluator(agent):
        result = agent.ask_with_reflection(question)
        print(f"\n{result.answer}\n")
        verdict = result.evaluation
        flag = "PASS" if result.passed else "NEEDS REVIEW"
        print(f"[reflection] {flag} (score={verdict.get('score')}, revisions={result.revisions})")
        for issue in verdict.get("issues", []):
            print(f"   - issue: {issue}")
        citations = result.citations
    else:
        ans = agent.ask(question)
        print(f"\n{ans.answer}\n")
        citations = ans.citations
    if citations:
        print("Sources:")
        for c in citations:
            print(f"  - {c['title']} ({c['url']})")
    print("-" * 60)


def main() -> None:
    load_dotenv(ENV_PATH)
    agent = build_agent()
    try:
        if len(sys.argv) > 1:
            show(agent, " ".join(sys.argv[1:]))
            return
        print("Arrivo (Foundry agent). Ask a settlement question. Ctrl-D to quit.")
        for line in sys.stdin:
            q = line.strip()
            if q:
                show(agent, q)
    finally:
        agent.close()


if __name__ == "__main__":
    main()
