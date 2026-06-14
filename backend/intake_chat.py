"""Conversational intake.

Instead of fixed multiple-choice questions, Arrivo interviews the newcomer in a
short, friendly chat so it can get to know them in their own words. The model
returns its next message plus its best structured read of the person so far; once
it has enough, it flags `ready` and we hand the extracted fields to
`journey.build_journey`.

Uses Azure OpenAI chat completions (configured in backend/.env). No knowledge
base / search is needed for onboarding, so this is intentionally lightweight.
"""
from __future__ import annotations

import json
import os

from openai import AzureOpenAI

SYSTEM_PROMPT = """\
You are Arrivo, a warm, upbeat onboarding guide for people who have JUST arrived in
Australia (focus: Sydney, NSW). In this first chat your job is simply to get to know
the newcomer so we can build them a personalised settlement plan. They may know
nothing about Australia — be friendly, reassuring and never overwhelming.

Through natural conversation, try to learn:
- visa_group: "student", "temp_work" (any working / sponsored / working-holiday visa),
  "pr" (permanent resident or citizen), or "unknown" if they're unsure.
- partner (true/false) and children (true/false): who is moving with them.
- weekly_budget: approximate weekly rent budget in AUD, as an integer.
- work_suburb: the Sydney suburb or area where they'll work or study (free text; "" if unknown).
- interests: any of ["groceries","food","outdoors","transport"] — what they'd like nearby.
- already_done: any of ["sim","bank","tfn","opal","health","home","licence"] — things they've
  already sorted, so we can skip them in the plan.

Also keep a running list of TOPICS the conversation has touched on, so the app can build
relevant panels live. Use ONLY these topic keys, and include a key as soon as it comes up
(yours or the user's), keeping all previously-raised topics:
["visa","household","budget","sim","bank","tfn","medicare","centrelink","housing",
 "licence","opal","transport","school","work"]

Separately, decide "show_map" (a boolean). Set it true ONLY when the user EXPLICITLY asks to
see or find places to go around them. Once true, keep it true.
  - TRUE examples: "show me good places to eat", "any nice beaches nearby?", "where can I do
    groceries?", "what's around Parramatta?", "where should I live?", "things to do near me".
  - FALSE examples (do NOT set true): "I'll be studying near Parramatta", "my budget is $600",
    "I need a TFN", "who's eligible for Medicare?" — mentioning a suburb or discussing budget,
    housing paperwork or entitlements is NOT a request to see places.

How to behave:
- Ask ONE friendly question at a time. Keep each reply to 1-3 short sentences.
- Use plain language and briefly explain Aussie terms when you use them
  (e.g. "a TFN — Tax File Number", "an Opal card — for trains and buses").
- Build on what they tell you; never re-ask something you already know or can infer.
- You do NOT need every detail. Once you know their visa situation, who's with them,
  a rough budget and roughly where they'll be, that's enough. Interests and
  already-sorted items are nice-to-have: ask about them once, then wrap up.
- When you have enough, set "ready": true. From then on KEEP chatting helpfully about
  settling in (how to do steps, what to bring, what's nearby), keep "ready" true, and keep
  updating "topics" and "fields" as new subjects come up so the side panel stays in sync.
- NEVER give migration or visa advice (eligibility, applications, conditions). If asked,
  gently explain that needs a registered migration agent (MARA, mara.gov.au) and steer
  back to settling in.

- For EVERY question you ask, include 3–5 short "suggestions" the user can tap as a reply.
  Each suggestion must be a complete, natural sentence (under ~60 chars) that directly
  answers the question you just asked. Tailor them to what's still unknown — e.g. visa options,
  household size, budget bands, Sydney areas, interests, or what's already sorted.
  If you are only acknowledging and not asking anything new, use [] or 1–2 optional follow-ups.

Respond with ONLY a JSON object in this exact shape:
{
  "reply": "<your next message to the user>",
  "ready": <true or false>,
  "show_map": <true or false>,
  "topics": [<cumulative topic keys raised so far>],
  "suggestions": [<2-5 short tappable reply options for your current question>],
  "fields": {
    "visa_group": <"student"|"temp_work"|"pr"|"unknown"|null>,
    "partner": <true|false|null>,
    "children": <true|false|null>,
    "weekly_budget": <integer or null>,
    "work_suburb": <string, "" if unknown>,
    "interests": [<zero or more of the allowed values>],
    "already_done": [<zero or more of the allowed values>]
  }
}
Use null when something is still unknown. Always include your best current read in "fields",
the full cumulative list in "topics", and relevant "suggestions" whenever you ask a question.
"""

# Fallback chips when the model omits suggestions (by what's still missing).
FALLBACK_SUGGESTIONS = {
    "visa": [
        "I'm on a student visa",
        "I have a work / sponsored visa",
        "I'm a permanent resident",
        "Working holiday visa",
        "I'm not sure yet",
    ],
    "household": [
        "Just me, on my own",
        "Me and my partner",
        "My family — we have kids",
    ],
    "budget": [
        "Under $400 a week",
        "Around $500–$600 a week",
        "About $700 a week",
        "$900 or more",
    ],
    "location": [
        "Near Parramatta",
        "Inner west / Strathfield",
        "South west / Liverpool",
        "Not sure yet",
    ],
    "interests": [
        "Groceries and shops nearby",
        "Good food and restaurants",
        "Beaches and parks",
        "Easy public transport",
    ],
    "done": [
        "Nothing yet — start fresh",
        "I already have a SIM and bank account",
        "I've got an Opal card",
        "I already have a place to live",
    ],
    "default": [
        "Tell me more about that",
        "What should I do first?",
        "I'm not sure — help me decide",
    ],
}

def _pick_fallback(fields: dict) -> list[str]:
    """Guess which topic we're still missing and return default tap options."""
    if not fields.get("visa_group"):
        return FALLBACK_SUGGESTIONS["visa"]
    if fields.get("partner") is None and fields.get("children") is None:
        return FALLBACK_SUGGESTIONS["household"]
    if not fields.get("weekly_budget"):
        return FALLBACK_SUGGESTIONS["budget"]
    if not fields.get("work_suburb"):
        return FALLBACK_SUGGESTIONS["location"]
    if not fields.get("interests"):
        return FALLBACK_SUGGESTIONS["interests"]
    if not fields.get("already_done"):
        return FALLBACK_SUGGESTIONS["done"]
    return FALLBACK_SUGGESTIONS["default"]


def _normalize_suggestions(raw, fields: dict, ready: bool) -> list[str]:
    out = []
    for s in raw or []:
        t = str(s).strip()
        if t and t not in out:
            out.append(t)
    if len(out) < 2 and not ready:
        for fb in _pick_fallback(fields or {}):
            if fb not in out:
                out.append(fb)
            if len(out) >= 4:
                break
    return out[:5]


TOPIC_KEYS = {
    "visa", "household", "budget", "sim", "bank", "tfn", "medicare", "centrelink",
    "housing", "licence", "opal", "transport", "school", "work",
}

_client: AzureOpenAI | None = None


def _aoai() -> AzureOpenAI:
    global _client
    if _client is None:
        _client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2024-10-21",
        )
    return _client


def chat(messages: list[dict]) -> dict:
    """Take the conversation so far, return the next assistant turn + extraction."""
    convo = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages or []:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            convo.append({"role": role, "content": content})
    if len(convo) == 1:  # nothing said yet — let the model open the conversation
        convo.append({"role": "user", "content": "(The user just opened the app.)"})

    resp = _aoai().chat.completions.create(
        model=os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"],
        response_format={"type": "json_object"},
        temperature=0.5,
        messages=convo,
    )
    try:
        data = json.loads(resp.choices[0].message.content)
    except (json.JSONDecodeError, TypeError):
        return {"reply": "Sorry, could you say that again?", "ready": False,
                "show_map": False, "topics": [], "suggestions": [], "fields": {}}

    fields = data.get("fields") or {}
    ready = bool(data.get("ready"))
    topics = [t for t in (data.get("topics") or []) if t in TOPIC_KEYS]
    suggestions = _normalize_suggestions(data.get("suggestions"), fields, ready)
    return {
        "reply": str(data.get("reply", "")).strip() or "Tell me a little more about your move.",
        "ready": ready,
        "show_map": bool(data.get("show_map")),
        "topics": topics,
        "suggestions": suggestions,
        "fields": fields,
    }
