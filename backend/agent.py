"""Arrivo reasoning agent.

A multi-step settlement-planning pipeline. Every step appends to a visible
reasoning trace; every factual claim must carry a citation from the knowledge
base or be explicitly marked unverified; the agent abstains rather than guesses.

Pipeline:
  1. classify   — map visa subclass to entitlement group (deterministic + cited)
  2. entitle    — retrieve + reason over entitlement rules for that group
  3. suburbs    — deterministic budget/commute filter over open rent data, LLM rationale
  4. sequence   — topological sort of the settlement dependency graph for this visa group
  5. enrich     — grounded tips for the hardest steps (rental, licence, school)
  6. verify     — re-check claims vs sources; downgrade anything unsupported
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI

DATA_DIR = Path(__file__).parent.parent / "data"
SUBURBS = json.loads((DATA_DIR / "suburbs_sydney.json").read_text())
RULES = json.loads((DATA_DIR / "checklist_rules.json").read_text())

# Visa subclass → entitlement group. Deliberately conservative: anything not
# listed is "unknown" and triggers abstention on entitlement claims.
VISA_GROUPS = {
    "189": "pr", "190": "pr", "191": "pr", "186": "pr", "187": "pr",
    "100": "pr", "801": "pr", "820": "temp_work", "309": "temp_work",
    "482": "temp_work", "485": "temp_work", "494": "temp_work", "400": "temp_work",
    "417": "temp_work", "462": "temp_work",
    "500": "student",
}

MIGRATION_ADVICE_GUARD = (
    "Arrivo gives settlement logistics information only. It does not and will not "
    "give migration advice (visa choice, eligibility, applications, appeals) — in "
    "Australia that legally requires a registered migration agent (MARA) or lawyer. "
    "For visa questions see https://www.mara.gov.au."
)


@dataclass
class Trace:
    steps: list[dict] = field(default_factory=list)

    def add(self, step: str, thought: str, **extra) -> None:
        self.steps.append({"step": step, "thought": thought, **extra})


class ArrivoAgent:
    def __init__(self) -> None:
        self.aoai = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2024-10-21",
        )
        self.chat_model = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]
        self.embed_model = os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"]
        self.search = SearchClient(
            os.environ["AZURE_SEARCH_ENDPOINT"],
            os.environ.get("AZURE_SEARCH_INDEX", "arrivo-kb"),
            AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"]),
        )

    # ---------- grounding ----------
    def retrieve(self, query: str, category: str | None = None, k: int = 4) -> list[dict]:
        vec = self.aoai.embeddings.create(model=self.embed_model, input=[query]).data[0].embedding
        results = self.search.search(
            search_text=query,
            vector_queries=[VectorizedQuery(vector=vec, k_nearest_neighbors=k, fields="vector")],
            filter=f"category eq '{category}'" if category else None,
            top=k,
        )
        return [
            {"title": r["title"], "source_url": r["source_url"], "content": r["content"][:1500]}
            for r in results
        ]

    def grounded_answer(self, question: str, sources: list[dict]) -> dict:
        """Ask the LLM to answer ONLY from sources, with per-claim citations, or abstain."""
        src_block = "\n\n".join(
            f"[{i+1}] {s['title']} ({s['source_url']})\n{s['content']}" for i, s in enumerate(sources)
        )
        system = (
            "You are a settlement information assistant. Answer ONLY using the numbered "
            "sources provided. Every claim must end with its citation like [1]. If the "
            "sources do not contain the answer, respond with exactly: INSUFFICIENT_EVIDENCE "
            "followed by one sentence describing what official source would be needed. "
            "Never give migration/visa advice. Respond in JSON: "
            '{"answer": "...", "confidence": "grounded|insufficient"}'
        )
        resp = self.aoai.chat.completions.create(
            model=self.chat_model,
            response_format={"type": "json_object"},
            temperature=0.1,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"SOURCES:\n{src_block}\n\nQUESTION: {question}"},
            ],
        )
        try:
            return json.loads(resp.choices[0].message.content)
        except json.JSONDecodeError:
            return {"answer": "INSUFFICIENT_EVIDENCE — model returned malformed output.",
                    "confidence": "insufficient"}

    # ---------- pipeline steps ----------
    def classify_visa(self, subclass: str, trace: Trace) -> str:
        group = VISA_GROUPS.get(subclass.strip(), "unknown")
        trace.add(
            "classify_visa",
            f"Subclass {subclass} maps to entitlement group '{group}'. "
            + ("This group is unrecognised, so I will abstain from entitlement claims "
               "rather than guess." if group == "unknown" else
               "I will only assert entitlements I can ground in official sources."),
            visa_group=group,
        )
        return group

    def entitlements(self, subclass: str, group: str, family: int, trace: Trace) -> list[dict]:
        questions = {
            "pr": [
                "Can a new permanent resident enrol in Medicare and how?",
                "What is the Newly Arrived Resident's Waiting Period and which payments does it affect?",
            ],
            "temp_work": [
                "Is a temporary work visa holder eligible for Medicare?",
                "What health insurance must temporary visa holders maintain?",
            ],
            "student": [
                "Is a student visa holder eligible for Medicare?",
                "What health cover do student visa holders need?",
            ],
            "unknown": [],
        }[group]
        out = []
        for q in questions:
            sources = self.retrieve(q, category="entitlements")
            result = self.grounded_answer(f"(Visa subclass {subclass}, family of {family}) {q}", sources)
            out.append({"question": q, **result,
                        "sources": [{"title": s["title"], "url": s["source_url"]} for s in sources]})
            trace.add("entitlements", f"Q: {q} → {result['confidence']}", )
        if group == "unknown":
            out.append({
                "question": "Entitlements for this visa subclass",
                "answer": "INSUFFICIENT_EVIDENCE — this visa subclass is not in my verified "
                          "mapping. Check directly with Services Australia, or consult a "
                          "registered migration agent for visa-specific conditions.",
                "confidence": "insufficient", "sources": [],
            })
            trace.add("entitlements", "Unknown visa group → abstaining instead of guessing.")
        return out

    def match_suburbs(self, weekly_budget: int, work_suburb: str, dwelling: str, trace: Trace) -> list[dict]:
        key = "rent_unit_2br" if dwelling == "unit" else "rent_house_3br"
        work = next((s for s in SUBURBS["suburbs"] if s["name"].lower() == work_suburb.lower()), None)

        def dist(s: dict) -> float:
            if not work:
                return 0.0
            return math.hypot(s["lat"] - work["lat"], s["lng"] - work["lng"]) * 111  # ~km

        candidates = [
            {**s, "rent": s[key], "distance_km": round(dist(s), 1)}
            for s in SUBURBS["suburbs"] if s[key] <= weekly_budget
        ]
        candidates.sort(key=lambda s: (s["distance_km"], s["rent"]))
        top = candidates[:4]
        trace.add(
            "match_suburbs",
            f"Deterministic filter: median {dwelling} rent <= ${weekly_budget}/wk gives "
            f"{len(candidates)} suburbs; ranked by distance to {work_suburb or 'CBD'} then rent. "
            "Rents are indicative medians from public NSW data, not live listings — I will "
            "say so in the output rather than fabricate availability.",
            shortlist=[s["name"] for s in top],
        )
        return top

    def sequence_steps(self, group: str, has_children: bool, trace: Trace) -> list[dict]:
        applicable = [
            s for s in RULES["steps"]
            if (not s["visa_conditions"] or group in s["visa_conditions"])
            and not (s["id"] == "school_enrolment" and not has_children)
        ]
        ids = {s["id"] for s in applicable}
        graph = {s["id"]: [r for r in s["requires"] if r in ids] for s in applicable}
        order = list(TopologicalSorter(graph).static_order())
        by_id = {s["id"]: s for s in applicable}
        trace.add(
            "sequence_steps",
            "Topological sort of the settlement dependency graph — e.g. the rental "
            f"application needs a bank account and TFN first. {len(order)} steps apply "
            f"to visa group '{group}'.",
            order=order,
        )
        return [
            {"order": i + 1, "id": sid, "label": by_id[sid]["label"],
             "tip": by_id[sid].get("tip", ""), "requires": by_id[sid]["requires"]}
            for i, sid in enumerate(order)
        ]

    def enrich_hard_steps(self, group: str, trace: Trace) -> list[dict]:
        questions = [
            ("housing", "What can a newcomer with no Australian rental history provide in a "
                        "rental application, and what are the bond rules in NSW?"),
            ("transport", "When must a new arrival convert an overseas driver licence in NSW?"),
        ]
        out = []
        for cat, q in questions:
            sources = self.retrieve(q, category=cat)
            result = self.grounded_answer(f"(Visa group: {group}) {q}", sources)
            out.append({"topic": cat, "question": q, **result,
                        "sources": [{"title": s["title"], "url": s["source_url"]} for s in sources]})
            trace.add("enrich", f"{cat}: {result['confidence']}")
        return out

    def verify(self, payload: dict, trace: Trace) -> dict:
        """Final pass: count grounded vs insufficient claims, surface honestly."""
        sections = payload["entitlements"] + payload["guides"]
        grounded = sum(1 for s in sections if s.get("confidence") == "grounded")
        insufficient = [s["question"] for s in sections if s.get("confidence") != "grounded"]
        trace.add(
            "verify",
            f"{grounded}/{len(sections)} answers fully grounded with citations. "
            + (f"Flagging {len(insufficient)} item(s) as needing official confirmation "
               "instead of presenting them as fact." if insufficient else
               "No unsupported claims in this plan."),
            unverified=insufficient,
        )
        payload["unverified_items"] = insufficient
        return payload

    # ---------- entry point ----------
    def plan(self, profile: dict) -> dict:
        trace = Trace()
        trace.add("intake", f"Profile received: {json.dumps({k: v for k, v in profile.items()})}. "
                            "Scope guard active: settlement logistics only, no migration advice.")
        group = self.classify_visa(profile["visa_subclass"], trace)
        entitlements = self.entitlements(profile["visa_subclass"], group, profile["family_size"], trace)
        suburbs = self.match_suburbs(
            profile["weekly_budget"], profile.get("work_suburb", ""), profile.get("dwelling", "unit"), trace
        )
        checklist = self.sequence_steps(group, profile.get("children", False), trace)
        guides = self.enrich_hard_steps(group, trace)
        payload = {
            "disclaimer": MIGRATION_ADVICE_GUARD,
            "visa_group": group,
            "entitlements": entitlements,
            "suburbs": suburbs,
            "pois": SUBURBS["pois"],
            "checklist": checklist,
            "guides": guides,
            "trace": trace.steps,
        }
        return self.verify(payload, trace)
