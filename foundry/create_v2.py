"""Create Arrivo's agents on the NEW Microsoft Foundry experience (v2 Agents).

Recreates the capabilities of the classic Assistants as v2 versioned agents
(``create_version`` + Responses API), so they appear in the new Foundry portal.

By default creates the single grounded agent + evaluator. Add ``--team`` to also
create the five specialists + the synthesiser orchestrator. The agent names are
written to backend/.env so the app/CLI pick them up.

Prereqs (in backend/.env from scripts/deploy.sh): FOUNDRY_PROJECT_ENDPOINT,
FOUNDRY_SEARCH_CONNECTION_NAME, AZURE_OPENAI_CHAT_DEPLOYMENT, AZURE_SEARCH_INDEX.
Auth: `az login` with the "Azure AI Developer" role on the project.

Usage:
    python foundry/create_v2.py            # single agent + evaluator
    python foundry/create_v2.py --team     # + specialists + orchestrator
    python foundry/create_v2.py --delete   # remove the v2 agents
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foundry.envfile import ENV_PATH, upsert_env  # noqa: E402
from foundry.specialists import SPECIALISTS  # noqa: E402
from foundry.v2 import (  # noqa: E402
    AGENT_NAME, EVALUATOR_NAME, ORCHESTRATOR_NAME, V2, FoundryV2Agent, FoundryV2Team,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create/delete Arrivo's v2 Foundry agents.")
    parser.add_argument("--team", action="store_true", help="Also create specialists + orchestrator.")
    parser.add_argument("--delete", action="store_true", help="Delete the v2 agents.")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    base = FoundryV2Agent()

    if args.delete:
        names = [AGENT_NAME, EVALUATOR_NAME, ORCHESTRATOR_NAME] + [s.name + V2 for s in SPECIALISTS]
        for n in names:
            try:
                base.project.agents.delete(n)
                print(f"   deleted {n}")
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                print(f"   skip {n} ({exc})")
        base.close()
        print(">> Deleted v2 agents.")
        return

    print(">> Creating v2 settlement agent + evaluator...")
    agent_name = base.create_agent()
    evaluator_name = base.create_evaluator()
    upsert_env("FOUNDRY_V2_AGENT_NAME", agent_name)
    upsert_env("FOUNDRY_V2_EVALUATOR_NAME", evaluator_name)
    print(f"   - {agent_name}")
    print(f"   - {evaluator_name}")

    if args.team:
        print(">> Creating v2 specialists + orchestrator...")
        team = FoundryV2Team(base=base)
        specialists = team.create_specialists()
        orchestrator = team.create_orchestrator()
        for key, name in specialists.items():
            print(f"   - {key:12s} {name}")
        print(f"   - orchestrator  {orchestrator}")
        upsert_env("FOUNDRY_V2_ORCHESTRATOR_NAME", orchestrator)
        upsert_env("FOUNDRY_V2_SPECIALIST_NAMES", json.dumps(specialists))

    print(f">> Saved v2 agent names in {ENV_PATH}")
    print(">> Try it:  python foundry/ask.py \"How do I enrol in Medicare as a new PR?\"")
    base.close()


if __name__ == "__main__":
    main()
