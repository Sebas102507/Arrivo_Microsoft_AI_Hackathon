"""Build a personalised settlement program from a guided conversation.

Two jobs:
  1. `assess_intake` — look at what the newcomer has told us so far (free text +
     answered questions) and decide what *key* information is still missing, then
     return friendly follow-up questions. The user may know nothing about
     Australia, so questions come with tappable options.
  2. `build_journey` — turn the completed profile into an ordered, phased program
     (skipping anything they've already sorted) plus a personalised map of nearby
     places (homes, groceries, food, outdoors, essentials).

Deterministic and fast (no LLM) so the UI stays reactive. The grounded Foundry
agent (/api/ask) handles deeper follow-up questions.
"""
from __future__ import annotations

import json
import math
import re
from graphlib import TopologicalSorter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SUBURBS = json.loads((_ROOT / "data" / "suburbs_sydney.json").read_text())
RULES = json.loads((_ROOT / "data" / "checklist_rules.json").read_text())
try:
    _SOURCES = json.loads((_ROOT / "ingestion" / "sources.json").read_text())
except (OSError, json.JSONDecodeError):
    _SOURCES = []

VISA_GROUPS = {
    "189": "pr", "190": "pr", "191": "pr", "186": "pr", "187": "pr",
    "100": "pr", "801": "pr", "820": "temp_work", "309": "temp_work",
    "482": "temp_work", "485": "temp_work", "494": "temp_work", "400": "temp_work",
    "417": "temp_work", "462": "temp_work", "500": "student",
}

GROUP_LABEL = {
    "pr": "Permanent resident", "temp_work": "Temporary work visa",
    "student": "Student visa", "unknown": "Visa not detected",
}
SUBCLASS_BY_GROUP = {"pr": "189", "temp_work": "482", "student": "500", "unknown": "999"}

# Map each settlement step to a knowledge category, an icon hint and a phase.
STEP_META = {
    "sim_card":          ("first_steps", "phone",   "Your first days"),
    "opal_card":         ("transport",   "transit", "Your first days"),
    "ovhc":              ("entitlements","health",  "Your first days"),
    "bank_account":      ("first_steps", "bank",    "Money & ID"),
    "tfn":               ("first_steps", "id",      "Money & ID"),
    "medicare":          ("entitlements","health",  "Health & support"),
    "centrelink":        ("entitlements","support", "Health & support"),
    "ahpra_or_skills":   ("general",     "work",    "Health & support"),
    "rental_application":("housing",     "home",    "Finding a home"),
    "lease_signed":      ("housing",     "key",     "Finding a home"),
    "utilities":         ("general",     "plug",    "Finding a home"),
    "drivers_licence":   ("transport",   "car",     "Settling in"),
    "school_enrolment":  ("education",   "school",  "Settling in"),
}
PHASE_WEEK = {"Your first days": 1, "Money & ID": 1, "Health & support": 2,
              "Finding a home": 3, "Settling in": 4}

# "Already sorted" answer ids -> step ids they let us skip.
DONE_TO_STEPS = {
    "sim": ["sim_card"],
    "bank": ["bank_account"],
    "tfn": ["tfn"],
    "opal": ["opal_card"],
    "health": ["medicare", "ovhc"],
    "home": ["rental_application", "lease_signed", "utilities"],
    "licence": ["drivers_licence"],
}

# Interest answer ids -> place categories to surface on the map.
INTEREST_TO_CATS = {
    "groceries": ["groceries"],
    "food": ["food"],
    "outdoors": ["beach", "park"],
    "transport": ["transport"],
}

BUDGET_OPTIONS = {
    "u400": 380, "b400": 500, "b550": 650, "b700": 820, "o900": 1050,
}
LOCATION_OPTIONS = {
    "parramatta": "Parramatta", "strathfield": "Strathfield", "liverpool": "Liverpool",
    "blacktown": "Blacktown", "chatswood": "Chatswood", "hurstville": "Hurstville",
    "unsure": "",
}


def _sources_for(category: str) -> list[dict]:
    out = [{"title": s["title"], "url": s["url"]} for s in _SOURCES if s.get("category") == category]
    if not out:
        out = [{"title": s["title"], "url": s["url"]} for s in _SOURCES if s.get("category") == "general"]
    return out[:2]


# ---------------------------------------------------------------------------
# Parsing free text
# ---------------------------------------------------------------------------
def parse_situation(text: str) -> dict:
    """Best-effort interpretation of free text. Also flags which facts we are
    confident about, so `assess_intake` knows what it still needs to ask."""
    t = f" {(text or '').lower()} "

    subclass, group, visa_detected = "999", "unknown", False
    for num in re.findall(r"\b(\d{3})\b", t):
        if num in VISA_GROUPS:
            subclass, group, visa_detected = num, VISA_GROUPS[num], True
            break
    if group == "unknown":
        if re.search(r"student|study|studying|uni\b|university|college", t):
            subclass, group, visa_detected = "500", "student", True
        elif re.search(r"graduate|post[- ]?study|485", t):
            subclass, group, visa_detected = "485", "temp_work", True
        elif re.search(r"working holiday|backpack|417|462", t):
            subclass, group, visa_detected = "417", "temp_work", True
        elif re.search(r"permanent|\bpr\b|skilled independent|189|190|186|citizen", t):
            subclass, group, visa_detected = "189", "pr", True
        elif re.search(r"work visa|sponsor|482|skills in demand|tss|employer", t):
            subclass, group, visa_detected = "482", "temp_work", True

    partner = bool(re.search(r"partner|spouse|wife|husband|married|fianc|couple|girlfriend|"
                             r"boyfriend|both of us|two of us|\bwe\b|\bwe're\b|\bwe are\b|together", t))
    children = bool(re.search(r"child|children|\bkid|kids|son|daughter|baby|toddler|school[- ]age|family of", t))
    solo = bool(re.search(r"\bsolo\b|\balone\b|by myself|just me|on my own|single\b", t))
    companions_detected = partner or children or solo
    family_size = 2 if partner else 1
    fam = re.search(r"family of (\d+)", t)
    if fam:
        family_size = max(1, min(12, int(fam.group(1))))

    budget, budget_detected = 650, False
    m = re.search(r"\$?\s?(\d{3,4})\s*(?:/|\s)?\s*(?:wk|week|pw|p/?w|per week|a week)", t)
    if not m:
        m = re.search(r"\$\s?(\d{3,4})\b", t)
    if m:
        budget, budget_detected = max(100, min(3000, int(m.group(1)))), True

    work_suburb, location_detected = "", False
    for s in SUBURBS["suburbs"]:
        if re.search(rf"\b{s['name'].lower()}\b", t):
            work_suburb, location_detected = s["name"], True
            break

    # Things they may already have sorted (so we can skip those steps).
    already = set()
    if re.search(r"opal", t):
        already.add("opal")
    if re.search(r"bank account|opened a bank|have a bank", t):
        already.add("bank")
    if re.search(r"\btfn\b|tax file", t):
        already.add("tfn")
    if re.search(r"\bsim\b|phone number|mobile number", t):
        already.add("sim")
    if re.search(r"medicare|health cover|health insurance|ovhc", t):
        already.add("health")
    if re.search(r"renting|already rent|have a place|have a home|signed a lease|apartment|my unit", t):
        already.add("home")
    if re.search(r"driver licen[cs]e|driving licen[cs]e", t):
        already.add("licence")

    return {
        "visa_subclass": subclass, "visa_group": group, "family_size": family_size,
        "children": children, "partner": partner, "weekly_budget": budget,
        "work_suburb": work_suburb, "dwelling": "house" if children else "unit",
        "interests": [], "already_done": sorted(already), "raw": (text or "").strip(),
        "_detected": {
            "visa": visa_detected, "companions": companions_detected,
            "budget": budget_detected, "location": location_detected,
        },
    }


# ---------------------------------------------------------------------------
# Conversational intake — work out what we still need to ask
# ---------------------------------------------------------------------------
def _opt(value: str, label: str, emoji: str = "") -> dict:
    return {"value": value, "label": label, "emoji": emoji}


QUESTION_BANK = {
    "visa": {
        "kind": "single",
        "title": "What kind of visa or status are you here on?",
        "help": "This decides what you can access — Medicare, work rights, licences. Not sure is fine.",
        "options": [
            _opt("student", "Student visa", "🎓"),
            _opt("work", "Working / sponsored visa", "💼"),
            _opt("whv", "Working holiday", "🌏"),
            _opt("pr", "Permanent resident", "🏡"),
            _opt("unsure", "I'm not sure", "🤔"),
        ],
    },
    "companions": {
        "kind": "single",
        "title": "Who's making the move with you?",
        "help": "We'll tailor housing and the to-do list to your household.",
        "options": [
            _opt("solo", "Just me", "🧍"),
            _opt("partner", "Me and my partner", "💑"),
            _opt("family", "My family (with kids)", "👨‍👩‍👧"),
        ],
    },
    "budget": {
        "kind": "single",
        "title": "Roughly what can you spend on rent each week?",
        "help": "A ballpark is fine — it helps us point you to suburbs you can actually afford.",
        "options": [
            _opt("u400", "Under $400", "💸"),
            _opt("b400", "$400 – $550", "💵"),
            _opt("b550", "$550 – $700", "💶"),
            _opt("b700", "$700 – $900", "💷"),
            _opt("o900", "$900+", "🤑"),
        ],
    },
    "location": {
        "kind": "single",
        "title": "Where will you mostly be — for work or study?",
        "help": "We'll find nearby suburbs and an easy commute. Pick the closest one.",
        "options": [
            _opt("parramatta", "Parramatta area", "🏙️"),
            _opt("strathfield", "Inner west / Strathfield", "🚉"),
            _opt("liverpool", "South west / Liverpool", "🛤️"),
            _opt("blacktown", "Western Sydney / Blacktown", "🏘️"),
            _opt("chatswood", "North shore / Chatswood", "🌳"),
            _opt("hurstville", "South / Hurstville", "🛍️"),
            _opt("unsure", "Not sure yet", "🤔"),
        ],
    },
    "interests": {
        "kind": "multi",
        "title": "What matters most to have nearby?",
        "help": "Pick any. We'll drop these on your map so you know where to go from day one.",
        "options": [
            _opt("groceries", "Groceries & shops", "🛒"),
            _opt("food", "Cheap eats & restaurants", "🍜"),
            _opt("outdoors", "Beaches, parks & chilling", "🏖️"),
            _opt("transport", "Easy public transport", "🚆"),
        ],
    },
    "done": {
        "kind": "multi",
        "title": "Have you already sorted any of these?",
        "help": "We'll skip whatever you've done, so your plan only shows what's left.",
        "options": [
            _opt("sim", "Australian SIM / phone", "📱"),
            _opt("bank", "Bank account", "🏦"),
            _opt("tfn", "Tax File Number (TFN)", "🧾"),
            _opt("opal", "Opal travel card", "🚇"),
            _opt("health", "Medicare / health cover", "🩺"),
            _opt("home", "A place to live", "🔑"),
            _opt("licence", "Driver licence", "🚗"),
            _opt("none", "None yet — start fresh", "🆕"),
        ],
    },
}

# Order we ask in.
QUESTION_ORDER = ["visa", "companions", "budget", "location", "interests", "done"]


def merge_answers(profile: dict, answers: dict | None) -> dict:
    """Fold answered questions back into the profile."""
    answers = answers or {}
    p = dict(profile)

    if "visa" in answers:
        mapping = {"student": ("500", "student"), "work": ("482", "temp_work"),
                   "whv": ("417", "temp_work"), "pr": ("189", "pr"), "unsure": ("999", "unknown")}
        p["visa_subclass"], p["visa_group"] = mapping.get(answers["visa"], ("999", "unknown"))

    if "companions" in answers:
        v = answers["companions"]
        p["partner"] = v in ("partner", "family")
        p["children"] = v == "family"
        p["family_size"] = 3 if v == "family" else (2 if v == "partner" else 1)
        p["dwelling"] = "house" if p["children"] else "unit"

    if "budget" in answers:
        p["weekly_budget"] = BUDGET_OPTIONS.get(answers["budget"], p.get("weekly_budget", 650))

    if "location" in answers:
        p["work_suburb"] = LOCATION_OPTIONS.get(answers["location"], "")

    if "interests" in answers:
        p["interests"] = [x for x in answers["interests"] if x in INTEREST_TO_CATS]

    if "done" in answers:
        sel = [x for x in answers["done"] if x in DONE_TO_STEPS]
        p["already_done"] = sorted(set(p.get("already_done", [])) | set(sel))

    return p


def coerce_profile(raw_text: str, fields: dict | None) -> dict:
    """Turn the fields extracted by the intake chat into a full internal profile.

    Anything the conversation didn't pin down falls back to a sensible default
    (or whatever we can still parse from the raw text)."""
    fields = fields or {}
    p = parse_situation(raw_text or "")

    group = fields.get("visa_group")
    if group in GROUP_LABEL:
        p["visa_group"] = group
        p["visa_subclass"] = SUBCLASS_BY_GROUP.get(group, p["visa_subclass"])

    if fields.get("partner") is not None:
        p["partner"] = bool(fields["partner"])
    if fields.get("children") is not None:
        p["children"] = bool(fields["children"])
    p["family_size"] = 3 if p["children"] else (2 if p["partner"] else 1)

    if fields.get("weekly_budget"):
        try:
            p["weekly_budget"] = max(100, min(3000, int(fields["weekly_budget"])))
        except (TypeError, ValueError):
            pass

    if fields.get("work_suburb"):
        name = str(fields["work_suburb"]).strip()
        match = next((s["name"] for s in SUBURBS["suburbs"] if s["name"].lower() == name.lower()), name)
        p["work_suburb"] = match

    p["dwelling"] = "house" if p["children"] else "unit"

    if isinstance(fields.get("interests"), list):
        p["interests"] = [x for x in fields["interests"] if x in INTEREST_TO_CATS]
    if isinstance(fields.get("already_done"), list):
        p["already_done"] = sorted(set(p.get("already_done", [])) |
                                   {x for x in fields["already_done"] if x in DONE_TO_STEPS})
    return p


def _needed_questions(profile: dict, answers: dict) -> list[str]:
    det = profile.get("_detected", {})
    need: list[str] = []
    if not det.get("visa") and "visa" not in answers:
        need.append("visa")
    if not det.get("companions") and "companions" not in answers:
        need.append("companions")
    if not det.get("budget") and "budget" not in answers:
        need.append("budget")
    if not det.get("location") and "location" not in answers:
        need.append("location")
    # Always confirm interests + what's already done: these personalise the
    # output and are rarely stated up front.
    if "interests" not in answers:
        need.append("interests")
    if "done" not in answers:
        need.append("done")
    return [q for q in QUESTION_ORDER if q in need]


def assess_intake(situation: str, answers: dict | None = None) -> dict:
    """Decide whether we know enough yet, and if not, which questions to ask."""
    answers = answers or {}
    profile = parse_situation(situation)
    needed = _needed_questions(profile, answers)
    questions = [{"id": qid, **QUESTION_BANK[qid]} for qid in needed]
    merged = merge_answers(profile, answers)
    return {
        "complete": len(questions) == 0,
        "questions": questions,
        "profile": _public_profile(merged),
    }


def _public_profile(p: dict) -> dict:
    out = {k: v for k, v in p.items() if not k.startswith("_")}
    out["visa_group"] = p["visa_group"]
    out["summary"] = _summary(p)
    return out


def _summary(p: dict) -> str:
    bits = [GROUP_LABEL[p["visa_group"]]]
    bits.append("with family" if p.get("children") else ("couple" if p.get("partner") else "solo"))
    bits.append(f"~${p['weekly_budget']}/wk")
    if p.get("work_suburb"):
        bits.append(f"near {p['work_suburb']}")
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# Building the plan
# ---------------------------------------------------------------------------
def _ordered_steps(group: str, has_children: bool, already_done: list[str]) -> list[dict]:
    skip = {sid for d in already_done for sid in DONE_TO_STEPS.get(d, [])}
    applicable = [
        s for s in RULES["steps"]
        if (not s["visa_conditions"] or group in s["visa_conditions"])
        and not (s["id"] == "school_enrolment" and not has_children)
        and s["id"] not in skip
    ]
    ids = {s["id"] for s in applicable}
    graph = {s["id"]: [r for r in s["requires"] if r in ids] for s in applicable}
    order = list(TopologicalSorter(graph).static_order())
    by_id = {s["id"]: s for s in applicable}
    label_by_id = {s["id"]: s["label"] for s in applicable}

    steps = []
    for i, sid in enumerate(order):
        rule = by_id[sid]
        category, icon, phase = STEP_META.get(sid, ("general", "dot", "Settling in"))
        steps.append({
            "order": i + 1,
            "id": sid,
            "title": rule["label"],
            "why": rule.get("tip") or rule.get("why_first", ""),
            "category": category,
            "icon": icon,
            "phase": phase,
            "week": PHASE_WEEK.get(phase, 4),
            "requires": [label_by_id.get(r, r) for r in rule["requires"] if r in ids],
            "sources": _sources_for(category),
        })
    return steps


def _catalog(group: str, has_children: bool, already_done: list[str]) -> dict:
    """Every settlement step with its details, so the conversation UI can surface
    a card for any topic the user raises — even ones not in their active plan
    (e.g. something they've already sorted, shown as done)."""
    skip = {sid for d in already_done for sid in DONE_TO_STEPS.get(d, [])}
    out: dict[str, dict] = {}
    for s in RULES["steps"]:
        sid = s["id"]
        category, icon, phase = STEP_META.get(sid, ("general", "dot", "Settling in"))
        applicable = (not s["visa_conditions"] or group in s["visa_conditions"])
        if sid == "school_enrolment" and not has_children:
            applicable = False
        out[sid] = {
            "id": sid,
            "title": s["label"],
            "why": s.get("tip") or s.get("why_first", ""),
            "category": category,
            "icon": icon,
            "phase": phase,
            "sources": _sources_for(category),
            "applicable": applicable,
            "done": sid in skip,
        }
    return out


def _match_suburbs(budget: int, work_suburb: str, dwelling: str) -> list[dict]:
    key = "rent_unit_2br" if dwelling == "unit" else "rent_house_3br"
    work = next((s for s in SUBURBS["suburbs"] if s["name"].lower() == (work_suburb or "").lower()), None)

    def dist(s: dict) -> float:
        if not work:
            return 0.0
        return round(math.hypot(s["lat"] - work["lat"], s["lng"] - work["lng"]) * 111, 1)

    def row(s: dict, over: bool) -> dict:
        return {"name": s["name"], "lat": s["lat"], "lng": s["lng"], "rent": s[key],
                "train_lines": s.get("train_lines", []), "notes": s.get("notes", ""),
                "distance_km": dist(s), "over_budget": over}

    within = [row(s, False) for s in SUBURBS["suburbs"] if s[key] <= budget]
    if within:
        within.sort(key=lambda s: (s["distance_km"], s["rent"]))
        return within[:5]
    # Nothing within budget — show the cheapest few so the map still helps.
    cheapest = sorted(SUBURBS["suburbs"], key=lambda s: s[key])[:4]
    return [row(s, True) for s in cheapest]


# Map data place "type" -> the broader category the UI groups/colours by.
TYPE_TO_GROUP = {
    "groceries": "groceries", "food": "food",
    "beach": "outdoors", "park": "outdoors",
    "transport": "essentials", "services": "essentials", "health": "essentials",
}


def _personalised_places(suburbs: list[dict], profile: dict) -> list[dict]:
    """Pick map places near the user's suburbs, filtered by what they care about.

    Always includes a few essentials. Adds the categories matching their stated
    interests, plus family-relevant spots (parks/health) when they have kids.
    City-wide highlights (beaches, big parks) appear only if they're into the
    outdoors, so two different users see two different maps.
    """
    names = {s["name"] for s in suburbs}
    cats = {"essentials"}
    for itr in profile.get("interests", []):
        for c in INTEREST_TO_CATS.get(itr, []):
            cats.add(TYPE_TO_GROUP.get(c, c))
    if profile.get("children"):
        cats |= {"outdoors", "essentials"}
    # If they picked no interests at all, still show the useful basics.
    if not profile.get("interests"):
        cats |= {"groceries", "essentials"}

    wants_outdoors = "outdoors" in cats
    out = []
    for p in SUBURBS.get("places", []):
        grp = TYPE_TO_GROUP.get(p["type"], p["type"])
        if grp not in cats:
            continue
        citywide = bool(p.get("citywide"))
        if citywide:
            # City-wide highlights only when the user is into the outdoors.
            if not (wants_outdoors and grp == "outdoors"):
                continue
        elif p.get("suburb") not in names:
            continue
        out.append({
            "name": p["name"], "type": p["type"], "group": grp,
            "suburb": p.get("suburb", "Sydney"), "lat": p["lat"], "lng": p["lng"],
            "desc": p.get("desc", ""),
        })
    return out


def build_journey(situation: str, answers: dict | None = None, fields: dict | None = None) -> dict:
    if fields is not None:
        profile = coerce_profile(situation, fields)
    else:
        base = parse_situation(situation)
        profile = merge_answers(base, answers)
    profile["summary"] = _summary(profile)

    steps = _ordered_steps(profile["visa_group"], profile["children"], profile.get("already_done", []))
    suburbs = _match_suburbs(profile["weekly_budget"], profile["work_suburb"], profile["dwelling"])
    places = _personalised_places(suburbs, profile)
    src_count = len({s["url"] for st in steps for s in st["sources"]})

    skipped = [sid for d in profile.get("already_done", []) for sid in DONE_TO_STEPS.get(d, [])]

    return {
        "profile": _public_profile(profile),
        "steps": steps,
        "catalog": _catalog(profile["visa_group"], profile["children"], profile.get("already_done", [])),
        "suburbs": suburbs,
        "places": places,
        "stats": {
            "total_steps": len(steps),
            "est_weeks": max([s["week"] for s in steps], default=0),
            "first_action": steps[0]["title"] if steps else "",
            "sources_count": src_count,
            "affordable_suburbs": len(suburbs),
            "places_count": len(places),
            "skipped_count": len(skipped),
        },
    }
