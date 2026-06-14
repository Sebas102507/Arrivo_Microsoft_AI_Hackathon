"""Conversational UI powered by Foundry v2 agents, with the orchestrator in charge.

The orchestrator (`arrivo-orchestrator-v2`) runs the whole conversation: it asks
the onboarding questions, keeps state, and produces the UI JSON payload. It only
DELEGATES to other agents when it actually needs grounded domain facts, which
keeps each turn fast and avoids rate limits:

  - Most onboarding turns  → 1 orchestrator call only.
  - A domain question      → orchestrator names the specialist(s) it needs, we
                              call ONLY those, then the orchestrator synthesises.
  - Optional evaluator pass (`arrivo-evaluator-v2`) on grounded answers when
    ARRIVO_USE_EVALUATOR=1.

Agents available to the orchestrator:
  arrivo-settlement-agent-v2 (general), and the five *-specialist-v2 agents
  (entitlements, first-steps, housing, transport, education).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from foundry.specialists import SPECIALISTS
from foundry.v2 import FoundryV2Team

_ROOT = Path(__file__).resolve().parent.parent
_DATA = json.loads((_ROOT / "data" / "suburbs_sydney.json").read_text())

_JSON_SCHEMA = """\
{
  "reply": "<your next message to the user, 1-3 short friendly sentences>",
  "ready": <true once you know visa, household, rough budget and area>,
  "show_map": <true only when the user explicitly asks to see places/locations>,
  "suggestions": [<3-5 short tappable reply options for your current question>],
  "topics": [<cumulative topic keys touched: visa, household, budget, sim, bank, tfn,
    medicare, centrelink, housing, licence, opal, transport, school, work>],
  "fields": {
    "visa_group": <"student"|"temp_work"|"pr"|"unknown"|null>,
    "visa_label": <human-readable visa label or null>,
    "partner": <true|false|null>,
    "children": <true|false|null>,
    "household_label": <e.g. "Solo", "Couple", "Family" or null>,
    "weekly_budget": <integer AUD rent budget or null>,
    "work_suburb": <string or "">,
    "interests": [<groceries|food|outdoors|transport>],
    "already_done": [<sim|bank|tfn|opal|health|home|licence>]
  },
  "journey": {
    "profile": { "summary": "<one line profile summary>" },
    "steps": [{
      "id": "<step id>", "order": <int>, "title": "<step title>",
      "why": "<why this step matters, grounded in specialist findings>",
      "phase": "<phase name>", "week": <int>,
      "requires": [<titles of prerequisite steps>],
      "sources": [{"title": "...", "url": "https://..."}]
    }],
    "catalog": { "<step_id>": {
      "id": "...", "title": "...", "why": "...",
      "applicable": <bool>, "done": <bool>,
      "sources": [{"title": "...", "url": "..."}]
    }},
    "highlight_steps": [<step ids to show as inline cards for current topics>],
    "suburbs": [{
      "name": "...", "lat": <float>, "lng": <float>, "rent": <int>,
      "distance_km": <float>, "over_budget": <bool>, "notes": "..."
    }],
    "places": [{
      "name": "...", "group": "<accommodation|groceries|food|outdoors|essentials>",
      "group_label": "<display label>", "lat": <float>, "lng": <float>,
      "suburb": "...", "desc": "..."
    }],
    "stats": {
      "total_steps": <int>, "est_weeks": <int>, "first_action": "<title or \"\">",
      "sources_count": <int>, "affordable_suburbs": <int>,
      "places_count": <int>, "skipped_count": <int>
    },
    "tips": [{ "text": "<actionable tip from specialists>", "color": "<hex color>" }],
    "welcome": { "title": "<headline>", "body": "<welcome paragraph>", "tag": "<optional tag>" },
    "provider": { "name": "<official body name>", "subtitle": "<location/context>",
      "verified_label": "<e.g. Verified source>" }
  },
    "citations": [{"title": "...", "url": "https://..."}],
  "consult_specialists": [<zero or more of: entitlements, first-steps, housing,
    transport, education — name ONLY the specialists whose official sources you
    need for THIS turn; leave empty [] during onboarding small-talk>]
}"""

_ORCHESTRATOR_PREFIX = """\
You are Arrivo's orchestrator and the single voice of the product. You run the
ENTIRE conversation with a newcomer to Australia (NSW): you greet them, ask the
onboarding questions, keep track of what you know, and build the UI payload.

You can DELEGATE to specialist agents, but only when you need official,
source-grounded facts you don't already have. Be economical — delegation is slow.

How to decide each turn:
- ONBOARDING / chit-chat (figuring out visa, household, budget, area, interests,
  what's already sorted): answer yourself. Set "consult_specialists": []. Ask ONE
  friendly question and give 3-5 short tappable "suggestions".
- DOMAIN QUESTION needing official facts (Medicare/Centrelink eligibility, TFN,
  bank account, renting/bonds, licence conversion, school enrolment): set
  "consult_specialists" to ONLY the relevant specialist key(s). You will be
  re-invoked with their findings to write the grounded answer.

Rules:
- One friendly question at a time; keep "reply" to 1-3 short sentences.
- Build "steps" in dependency order; cite official sources you were given.
- Select "suburbs"/"places" ONLY from REFERENCE DATA (match lat/lng exactly).
- "tips" and any factual claim must be grounded in specialist findings you were
  given (or empty during onboarding) — never invent facts, URLs, or figures.
- NEVER give migration/visa advice; refer to MARA (mara.gov.au) if asked.
- Respond with ONLY valid JSON matching this schema (no markdown fences):
"""

# The exact Foundry v2 agents this app drives. These names are authoritative —
# we pin them here so the frontend always calls all eight, regardless of env vars.
MAIN_AGENT = "arrivo-settlement-agent-v2"
EVALUATOR_AGENT = "arrivo-evaluator-v2"
ORCHESTRATOR_AGENT = "arrivo-orchestrator-v2"
SPECIALIST_AGENTS = {spec.key: spec.name + "-v2" for spec in SPECIALISTS}
ALL_AGENTS = [MAIN_AGENT, *SPECIALIST_AGENTS.values(), ORCHESTRATOR_AGENT, EVALUATOR_AGENT]

_team: FoundryV2Team | None = None


def _get_team() -> FoundryV2Team:
    """Build the team with every agent name pinned to its exact Foundry v2 name."""
    global _team
    if _team is None:
        if not os.environ.get("FOUNDRY_PROJECT_ENDPOINT"):
            raise RuntimeError(
                "Foundry agents not configured. Set FOUNDRY_PROJECT_ENDPOINT and run "
                "`python foundry/create_v2.py --team`."
            )
        _team = FoundryV2Team()
        # Pin the orchestrator, main agent, evaluator and specialists explicitly so
        # nothing falls back to a single agent or skips the evaluator.
        _team.orchestrator_name = ORCHESTRATOR_AGENT
        _team.base.agent_name = MAIN_AGENT
        _team.base.evaluator_name = EVALUATOR_AGENT
        _team.specialist_names = dict(SPECIALIST_AGENTS)
    return _team


def _run(team: FoundryV2Team, agent_name: str, prompt: str, *, retries: int = 3):
    """Run an agent with exponential backoff on rate-limit (429) errors."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            return team.base.run(agent_name, prompt)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            is_rate = "429" in msg or "rate_limit" in msg.lower() or "too many requests" in msg.lower()
            if is_rate and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _format_conversation(messages: list[dict]) -> str:
    lines = []
    for m in messages or []:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines) if lines else "(No messages yet.)"


def _reference_data() -> str:
    """Condensed suburb/place data for the orchestrator to select from."""
    suburbs = [
        {
            "name": s["name"], "lat": s["lat"], "lng": s["lng"],
            "rent_unit_2br": s["rent_unit_2br"], "rent_house_3br": s["rent_house_3br"],
            "notes": s.get("notes", ""),
        }
        for s in _DATA.get("suburbs", [])
    ]
    places = [
        {
            "name": p["name"], "type": p["type"], "suburb": p.get("suburb", ""),
            "lat": p["lat"], "lng": p["lng"], "desc": p.get("desc", ""),
            "citywide": bool(p.get("citywide")),
        }
        for p in _DATA.get("places", [])
    ]
    return json.dumps({"suburbs": suburbs, "places": places}, indent=0)


def _specialist_question(spec_key: str, conv: str, latest_user: str) -> str:
    return (
        f"Onboarding conversation so far:\n{conv}\n\n"
        f"Latest user message: {latest_user or '(user just opened the app)'}\n\n"
        f"As the {spec_key} specialist, what settlement guidance from your knowledge "
        "base is relevant RIGHT NOW? Include ordered steps, eligibility notes, and "
        "citations. If nothing applies yet, say what you'll help with once you know more."
    )


def _gather_specialists(team: FoundryV2Team, keys: list[str], conv: str, latest_user: str) -> tuple[str, list[str]]:
    """Call ONLY the specialists the orchestrator asked for. Returns (findings, agent_names)."""
    blocks, used = [], []
    for key in keys:
        name = SPECIALIST_AGENTS.get(key)
        if not name:
            continue
        ans = _run(team, name, _specialist_question(key, conv, latest_user))
        sources = "; ".join(f"{c['title']} ({c['url']})" for c in ans.citations) or "(no citations)"
        blocks.append(f"### {name}\n{ans.answer}\nSOURCES: {sources}")
        used.append(name)
    return ("\n\n".join(blocks) if blocks else "(no specialist findings)"), used


def _parse_json(text: str) -> dict:
    candidate = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return {}


def _latest_user(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return (m.get("content") or "").strip()
    return ""


def _empty_journey() -> dict:
    return {
        "profile": {"summary": ""},
        "steps": [], "catalog": {}, "highlight_steps": [],
        "suburbs": [], "places": [],
        "stats": {
            "total_steps": 0, "est_weeks": 0, "first_action": "",
            "sources_count": 0, "affordable_suburbs": 0,
            "places_count": 0, "skipped_count": 0,
        },
        "tips": [],
        "welcome": {"title": "", "body": "", "tag": ""},
        "provider": {"name": "", "subtitle": "", "verified_label": ""},
    }


def _normalize(data: dict, all_citations: list[dict]) -> dict:
    fields = data.get("fields") or {}
    journey = data.get("journey") or {}
    if not journey.get("profile"):
        journey["profile"] = {"summary": ""}
    for key in ("steps", "suburbs", "places", "tips"):
        journey.setdefault(key, [])
    journey.setdefault("catalog", {})
    journey.setdefault("highlight_steps", [])
    journey.setdefault("stats", _empty_journey()["stats"])
    journey.setdefault("welcome", {"title": "", "body": "", "tag": ""})
    journey.setdefault("provider", {"name": "", "subtitle": "", "verified_label": ""})

    citations = data.get("citations") or all_citations
    seen, unique = set(), []
    for c in citations:
        key = (c.get("title"), c.get("url"))
        if key not in seen and c.get("url"):
            seen.add(key)
            unique.append({"title": c.get("title", ""), "url": c["url"]})

    return {
        "reply": str(data.get("reply", "")).strip(),
        "ready": bool(data.get("ready")),
        "show_map": bool(data.get("show_map")),
        "suggestions": [str(s).strip() for s in (data.get("suggestions") or []) if str(s).strip()][:5],
        "topics": list(data.get("topics") or []),
        "fields": fields,
        "journey": journey,
        "citations": unique,
        "agents_used": data.get("agents_used", []),
    }


def _build_prompt(conv: str, user_ctx: str, findings: str | None) -> str:
    parts = [
        _ORCHESTRATOR_PREFIX, _JSON_SCHEMA, "",
        f"CONVERSATION:\n{conv}", "", user_ctx, "",
        f"REFERENCE DATA (select suburbs/places from here only):\n{_reference_data()}",
    ]
    if findings:
        parts += ["", f"SPECIALIST FINDINGS:\n{findings}"]
    parts += ["", "Produce the JSON object now."]
    return "\n".join(parts)


def chat(messages: list[dict]) -> dict:
    """Orchestrator-driven turn.

    The orchestrator answers directly during onboarding (1 call). When it needs
    official facts it returns "consult_specialists"; we call ONLY those, then the
    orchestrator synthesises the grounded answer. An optional evaluator pass runs
    on grounded answers when ARRIVO_USE_EVALUATOR=1.
    """
    team = _get_team()
    conv = _format_conversation(messages)
    latest = _latest_user(messages)
    is_opening = not latest and not any(m.get("role") == "user" for m in (messages or []))

    user_ctx = (
        "(The user just opened Arrivo — welcome them warmly and ask your first question.)"
        if is_opening else f"Latest user message:\n{latest}"
    )

    agents_used: list[str] = [ORCHESTRATOR_AGENT]

    # Phase 1 — orchestrator drives the turn and decides whether to delegate.
    draft = _run(team, ORCHESTRATOR_AGENT, _build_prompt(conv, user_ctx, None))
    parsed = _parse_json(draft.answer)
    all_citations = list(draft.citations)
    reply_text = parsed.get("reply") or draft.answer

    consult = [k for k in (parsed.get("consult_specialists") or []) if k in SPECIALIST_AGENTS]

    # Phase 2 — only if the orchestrator asked for grounded domain facts.
    if consult:
        findings, spec_used = _gather_specialists(team, consult, conv, latest)
        agents_used += spec_used
        agents_used.append(ORCHESTRATOR_AGENT)
        synth = _run(team, ORCHESTRATOR_AGENT, _build_prompt(conv, user_ctx, findings))
        reparsed = _parse_json(synth.answer)
        if reparsed.get("reply"):
            parsed = reparsed
            reply_text = reparsed["reply"]
            all_citations = synth.citations or all_citations

        # Optional evaluator pass — only on grounded answers, only when enabled.
        if os.environ.get("ARRIVO_USE_EVALUATOR", "").lower() in ("1", "true", "yes"):
            agents_used.append(EVALUATOR_AGENT)
            verdict = team.base.evaluate(latest or "Settlement question", reply_text)
            if not verdict.get("pass"):
                issues = "; ".join(verdict.get("issues", [])) or "(see suggestions)"
                revise = _run(team, ORCHESTRATOR_AGENT,
                              f"{_build_prompt(conv, user_ctx, findings)}\n\n"
                              f"Your previous JSON reply was:\n{reply_text}\n\n"
                              f"Evaluator flagged: {issues}\nFixes: {verdict.get('suggestions', '')}\n"
                              "Rewrite the full JSON object addressing these issues.")
                rev = _parse_json(revise.answer)
                if rev.get("reply"):
                    parsed = rev
                    reply_text = rev["reply"]
                    all_citations = revise.citations or all_citations

    if not parsed.get("reply"):
        parsed["reply"] = reply_text or "Tell me a bit about your move — I'm here to help you settle in."

    parsed["agents_used"] = agents_used
    return _normalize(parsed, all_citations)
