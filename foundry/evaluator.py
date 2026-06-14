"""Evaluator (reflection) agent for Arrivo.

A separate Foundry agent that critiques a draft answer BEFORE it is returned to
the user. It has its own Azure AI Search tool over the whole `arrivo-kb` index so
it can independently check that claims are actually supported by official sources —
this is a real grounding check, not just a style pass.

It returns a strict JSON verdict that the reflection loop in ``client.py`` uses to
decide whether to accept the answer or send it back for one revision.
"""
from __future__ import annotations

import json
import re

EVALUATOR_NAME = "arrivo-evaluator"

EVALUATOR_INSTRUCTIONS = """\
You are Arrivo's answer evaluator. You are given a user QUESTION and a DRAFT ANSWER
written by another agent for someone settling in Australia (NSW). Your job is to
judge the draft BEFORE it reaches the user. Use your Azure AI Search knowledge tool
over `arrivo-kb` (official Australian government settlement sources) to independently
verify the factual claims.

Check the draft against these criteria:
1. GROUNDED — every factual claim (eligibility rules, dollar amounts, dates,
   deadlines, processes) is supported by the official sources and carries a citation.
   Flag any claim you cannot find support for, and any URL or figure that looks invented.
2. IN SCOPE — the draft stays within settlement logistics and gives NO migration or
   visa advice (visa choice, eligibility, applications, conditions, appeals). Giving
   such advice is a hard FAIL.
3. HONEST ABSTENTION — if the sources don't cover something, the draft says so
   (INSUFFICIENT_EVIDENCE) instead of guessing. Confidently stated unsupported claims
   are a FAIL; a correct abstention is a PASS.
4. ORDER & CLARITY — when steps depend on each other, they are in a sensible order.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{
  "pass": true or false,
  "score": 0.0 to 1.0,
  "issues": ["short, specific problems found"],
  "suggestions": "one paragraph of concrete fixes the author should make",
  "unsupported_claims": ["claims you could not verify in the sources"]
}
Set "pass" to false if there is any out-of-scope migration advice, any confidently
stated unsupported claim, or any fabricated source/figure.
"""


def parse_verdict(text: str) -> dict:
    """Best-effort extraction of the evaluator's JSON verdict from its message."""
    candidate = text.strip()
    # Strip ```json fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        # Otherwise take the outermost {...}.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        # Couldn't parse — fail open to "needs review" rather than silently passing.
        return {
            "pass": False,
            "score": 0.0,
            "issues": ["Evaluator returned unparseable output."],
            "suggestions": text.strip()[:500],
            "unsupported_claims": [],
            "_unparsed": True,
        }
    data.setdefault("pass", False)
    data.setdefault("score", 0.0)
    data.setdefault("issues", [])
    data.setdefault("suggestions", "")
    data.setdefault("unsupported_claims", [])
    return data
