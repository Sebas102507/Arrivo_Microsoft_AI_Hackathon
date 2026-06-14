"""Arrivo on the NEW Microsoft Foundry experience (v2 "Agents").

The classic build (``client.py`` / ``team.py``) uses the Assistants API
(threads + runs). The new Foundry experience uses **versioned Agents** created
with ``project.agents.create_version`` and run through the **Responses API**
(conversations + responses). This module re-implements Arrivo's capabilities on
that new surface:

  * ``FoundryV2Agent``  — a single grounded prompt agent + an evaluator agent,
    with the same draft → evaluate → revise reflection loop.
  * ``FoundryV2Team``   — five domain specialists (each a real v2 agent grounded
    on its slice of arrivo-kb) plus a synthesiser/orchestrator agent. Because v2
    has no ``ConnectedAgentTool`` (multi-agent is the preview A2A tool), the
    orchestration is done in code: call the specialists, then have the
    orchestrator agent merge their cited answers.

Classic → new mapping: agents→create_version, threads→conversations, runs→responses.
"""
from __future__ import annotations

import os

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchTool,
    AzureAISearchToolResource,
    ConnectionType,
    PromptAgentDefinition,
)

from .common import (
    QUERY_TYPES,
    TITLE_TO_URL,
    AgentAnswer,
    ReflectionResult,
    extract_inline_citations,
)
from .evaluator import EVALUATOR_INSTRUCTIONS, parse_verdict
from .instructions import INSTRUCTIONS
from .specialists import SPECIALISTS

ORCHESTRATOR_INSTRUCTIONS = """\
You are Arrivo's lead settlement coordinator for people who have just arrived in
Australia (focused on NSW). You synthesise findings from specialist agents into one
clear, correctly ordered answer.

# Specialist domains
- entitlements  — Medicare, Centrelink, newly-arrived waiting periods
- first_steps   — Tax File Number, opening a bank account
- housing       — renting in NSW, bonds, tenancy
- transport     — converting an overseas licence, Opal/public transport
- education     — enrolling children in NSW public schools

# How to work
1. When steps depend on each other, present them in the right ORDER and say why
   (e.g. a rental application wants payslips, which want a Tax File Number, which
   wants an Australian address and phone number).
2. Preserve every source citation from the specialists — each factual claim stays
   attributed to an official source.
3. If a specialist returns INSUFFICIENT_EVIDENCE, pass that honesty through: say
   what is unknown and which official source to check. Never invent facts.

# Hard scope boundary (legal)
You provide settlement logistics only — never migration or visa advice (visa choice,
eligibility, applications, conditions, appeals). That legally requires a registered
migration agent (MARA). If asked, decline briefly, refer to https://www.mara.gov.au,
and offer to help with settlement logistics instead.

Be concise, welcoming, and practical. Assume the reader is new to Australia.
"""

# v2 agents are referenced by name; suffix keeps them distinct from the classic ones.
V2 = "-v2"
AGENT_NAME = "arrivo-settlement-agent" + V2
EVALUATOR_NAME = "arrivo-evaluator" + V2
ORCHESTRATOR_NAME = "arrivo-orchestrator" + V2


class FoundryV2Agent:
    def __init__(
        self,
        project_endpoint: str | None = None,
        model: str | None = None,
        index_name: str | None = None,
        connection_name: str | None = None,
        query_type: str | None = None,
        agent_name: str | None = None,
        evaluator_name: str | None = None,
    ) -> None:
        self.project_endpoint = project_endpoint or os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        self.model = model or os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
        self.index_name = index_name or os.environ.get("AZURE_SEARCH_INDEX", "arrivo-kb")
        self.connection_name = connection_name or os.environ.get("FOUNDRY_SEARCH_CONNECTION_NAME")
        self.query_type = QUERY_TYPES[
            (query_type or os.environ.get("FOUNDRY_SEARCH_QUERY_TYPE", "simple")).lower()
        ]
        self.agent_name = agent_name or os.environ.get("FOUNDRY_V2_AGENT_NAME", AGENT_NAME)
        self.evaluator_name = evaluator_name or os.environ.get("FOUNDRY_V2_EVALUATOR_NAME")
        self._credential = DefaultAzureCredential()
        self._project: AIProjectClient | None = None
        self._oai = None
        self._conn_id: str | None = None

    # ---------- clients ----------
    @property
    def project(self) -> AIProjectClient:
        if self._project is None:
            self._project = AIProjectClient(endpoint=self.project_endpoint, credential=self._credential)
        return self._project

    @property
    def oai(self):
        """OpenAI-compatible client for the Responses + Conversations APIs."""
        if self._oai is None:
            self._oai = self.project.get_openai_client()
        return self._oai

    def resolve_connection_id(self) -> str:
        if self._conn_id:
            return self._conn_id
        if self.connection_name:
            self._conn_id = self.project.connections.get(name=self.connection_name).id
            return self._conn_id
        try:
            self._conn_id = self.project.connections.get_default(ConnectionType.AZURE_AI_SEARCH).id
            return self._conn_id
        except Exception:  # noqa: BLE001
            for conn in self.project.connections.list():
                if str(getattr(conn, "type", "")) in (
                    str(ConnectionType.AZURE_AI_SEARCH), "CognitiveSearch", "AzureAISearch"
                ):
                    self._conn_id = conn.id
                    return self._conn_id
        raise RuntimeError("No Azure AI Search connection found on the Foundry project.")

    def _search_tool(self, category_filter: str = "") -> AzureAISearchTool:
        odata = f"category eq '{category_filter}'" if category_filter else ""
        return AzureAISearchTool(
            azure_ai_search=AzureAISearchToolResource(
                indexes=[
                    AISearchIndexResource(
                        project_connection_id=self.resolve_connection_id(),
                        index_name=self.index_name,
                        query_type=self.query_type,
                        filter=odata,
                        top_k=5,
                    )
                ]
            )
        )

    # ---------- lifecycle ----------
    def create_prompt_agent(self, name: str, instructions: str, category_filter: str = "",
                            grounded: bool = True) -> str:
        """Create/replace a v2 prompt agent version; returns its name (the reference)."""
        tools = [self._search_tool(category_filter)] if grounded else []
        agent = self.project.agents.create_version(
            agent_name=name,
            definition=PromptAgentDefinition(
                model=self.model,
                instructions=instructions,
                tools=tools,
            ),
            description="Arrivo settlement agent (Foundry v2).",
        )
        return agent.name

    def create_agent(self) -> str:
        self.agent_name = self.create_prompt_agent(self.agent_name, INSTRUCTIONS)
        return self.agent_name

    def create_evaluator(self) -> str:
        self.evaluator_name = self.create_prompt_agent(EVALUATOR_NAME, EVALUATOR_INSTRUCTIONS)
        return self.evaluator_name

    # ---------- run ----------
    @staticmethod
    def _content_text(content) -> str:
        val = getattr(content, "text", "") or ""
        return val if isinstance(val, str) else (getattr(val, "value", "") or "")

    def _parse_response(self, resp) -> AgentAnswer:
        citations: list[dict] = []
        parts: list[str] = []
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", None) or []:
                if getattr(content, "type", None) != "output_text":
                    continue
                value = self._content_text(content)
                spans = []  # (start, end, title, url) to rewrite the inline markers as links
                for ann in getattr(content, "annotations", None) or []:
                    if getattr(ann, "type", None) != "url_citation":
                        continue
                    title = getattr(ann, "title", "") or ""
                    # Our KB title→gov-URL map is authoritative; the tool otherwise returns
                    # the search service URL or a placeholder.
                    url = TITLE_TO_URL.get(title) or getattr(ann, "url", "") or ""
                    citations.append({"title": title or url, "url": url})
                    si, ei = getattr(ann, "start_index", None), getattr(ann, "end_index", None)
                    if isinstance(si, int) and isinstance(ei, int) and 0 <= si <= ei <= len(value):
                        label = title or url
                        spans.append((si, ei, f" [{label}]({url})" if label else ""))
                for si, ei, repl in sorted(spans, key=lambda x: x[0], reverse=True):
                    value = value[:si] + repl + value[ei:]
                parts.append(value)
        text = ("\n".join(parts).strip()) or (getattr(resp, "output_text", "") or "").strip()
        seen, unique = set(), []
        for c in citations:
            key = (c["title"], c["url"])
            if key not in seen:
                seen.add(key)
                unique.append(c)
        if not unique:
            unique = extract_inline_citations(text)
        return AgentAnswer(answer=text, citations=unique, run_status="completed")

    def run(self, agent_name: str, question: str) -> AgentAnswer:
        """Run one question through a v2 agent via the Responses API."""
        conv = self.oai.conversations.create(
            items=[{"type": "message", "role": "user", "content": question}]
        )
        resp = self.oai.responses.create(
            conversation=conv.id,
            extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
        )
        try:
            return self._parse_response(resp)
        finally:
            try:
                self.oai.conversations.delete(conversation_id=conv.id)
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass

    def ask(self, question: str) -> AgentAnswer:
        return self.run(self.agent_name, question)

    # ---------- reflection ----------
    def evaluate(self, question: str, answer: str) -> dict:
        if not self.evaluator_name:
            raise RuntimeError("No v2 evaluator. Run create_v2.py or set FOUNDRY_V2_EVALUATOR_NAME.")
        prompt = (
            f"QUESTION:\n{question}\n\nDRAFT ANSWER:\n{answer}\n\n"
            "Evaluate the draft and respond with ONLY the JSON verdict."
        )
        return parse_verdict(self.run(self.evaluator_name, prompt).answer)

    def _revise(self, question: str, prev_answer: str, verdict: dict, agent_name: str) -> AgentAnswer:
        issues = "; ".join(verdict.get("issues", [])) or "(see suggestions)"
        prompt = (
            f"You previously answered this question:\n{question}\n\nYour draft was:\n{prev_answer}\n\n"
            f"An evaluator flagged these issues: {issues}\nSuggested fixes: {verdict.get('suggestions', '')}\n\n"
            "Produce an improved answer. Re-check the knowledge base, keep a citation on every "
            "factual claim, drop or mark anything you cannot support, and stay within settlement "
            "logistics (no migration/visa advice)."
        )
        return self.run(agent_name, prompt)

    def ask_with_reflection(self, question: str, max_revisions: int = 1,
                            answer_agent: str | None = None) -> ReflectionResult:
        agent_name = answer_agent or self.agent_name
        current = self.run(agent_name, question)
        trace: list[dict] = []
        verdict: dict = {}
        revisions = 0
        for attempt in range(max_revisions + 1):
            verdict = self.evaluate(question, current.answer)
            trace.append({"attempt": attempt, "answer": current.answer, "evaluation": verdict})
            if verdict.get("pass") or attempt == max_revisions:
                break
            current = self._revise(question, current.answer, verdict, agent_name)
            revisions += 1
        return ReflectionResult(
            answer=current.answer, citations=current.citations,
            passed=bool(verdict.get("pass")), evaluation=verdict,
            revisions=revisions, trace=trace,
        )

    def close(self) -> None:
        if self._oai is not None:
            try:
                self._oai.close()
            except Exception:  # noqa: BLE001
                pass
            self._oai = None
        if self._project is not None:
            self._project.close()
            self._project = None


# v2 has no ConnectedAgentTool, so the orchestrator synthesises from specialist
# outputs we gather in code. Its instructions reuse the team policy but make clear
# it works from the provided specialist findings rather than calling tools itself.
ORCHESTRATOR_V2_INSTRUCTIONS = ORCHESTRATOR_INSTRUCTIONS + """

# v2 note
You will be given the user's question together with findings already gathered from
your specialist agents (each with its sources). Synthesise those findings into one
coherent, correctly ordered answer. Preserve every source citation. Do not invent
anything beyond what the specialists provided; if a needed area is missing or a
specialist abstained, say so honestly.
"""


class FoundryV2Team:
    """Code-orchestrated multi-agent team on the new Foundry experience."""

    def __init__(self, base: FoundryV2Agent | None = None) -> None:
        self.base = base or FoundryV2Agent()
        self.specialist_names: dict[str, str] = {}
        self.orchestrator_name = os.environ.get("FOUNDRY_V2_ORCHESTRATOR_NAME", ORCHESTRATOR_NAME)

    def create_specialists(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for spec in SPECIALISTS:
            names[spec.key] = self.base.create_prompt_agent(
                spec.name + V2, spec.full_instructions, category_filter=spec.category, grounded=True
            )
        self.specialist_names = names
        return names

    def create_orchestrator(self) -> str:
        # No search tool: it synthesises from specialist findings passed in the prompt.
        self.orchestrator_name = self.base.create_prompt_agent(
            ORCHESTRATOR_NAME, ORCHESTRATOR_V2_INSTRUCTIONS, grounded=False
        )
        return self.orchestrator_name

    def create_team(self) -> dict:
        self.create_specialists()
        self.create_orchestrator()
        return {"orchestrator": self.orchestrator_name, "specialists": self.specialist_names}

    def _gather(self, question: str) -> str:
        """Ask each specialist and assemble their findings for the orchestrator."""
        blocks = []
        for spec in SPECIALISTS:
            name = self.specialist_names.get(spec.key) or (spec.name + V2)
            ans = self.base.run(name, question)
            sources = "; ".join(f"{c['title']} ({c['url']})" for c in ans.citations) or "(no citations)"
            blocks.append(f"### {spec.key} specialist\n{ans.answer}\nSOURCES: {sources}")
        return "\n\n".join(blocks)

    def ask(self, question: str) -> AgentAnswer:
        findings = self._gather(question)
        synthesis_input = (
            f"USER QUESTION:\n{question}\n\nSPECIALIST FINDINGS:\n{findings}\n\n"
            "Now write the final answer for the user."
        )
        return self.base.run(self.orchestrator_name, synthesis_input)

    def ask_with_reflection(self, question: str, max_revisions: int = 1) -> ReflectionResult:
        findings = self._gather(question)
        synthesis_input = (
            f"USER QUESTION:\n{question}\n\nSPECIALIST FINDINGS:\n{findings}\n\n"
            "Now write the final answer for the user."
        )
        current = self.base.run(self.orchestrator_name, synthesis_input)
        trace: list[dict] = []
        verdict: dict = {}
        revisions = 0
        for attempt in range(max_revisions + 1):
            verdict = self.base.evaluate(question, current.answer)
            trace.append({"attempt": attempt, "answer": current.answer, "evaluation": verdict})
            if verdict.get("pass") or attempt == max_revisions:
                break
            issues = "; ".join(verdict.get("issues", [])) or "(see suggestions)"
            revise_input = (
                f"{synthesis_input}\n\nYour previous synthesis was:\n{current.answer}\n\n"
                f"An evaluator flagged: {issues}\nFixes: {verdict.get('suggestions', '')}\n"
                "Rewrite the final answer addressing these, keeping all citations."
            )
            current = self.base.run(self.orchestrator_name, revise_input)
            revisions += 1
        return ReflectionResult(
            answer=current.answer, citations=current.citations,
            passed=bool(verdict.get("pass")), evaluation=verdict,
            revisions=revisions, trace=trace,
        )

    def close(self) -> None:
        self.base.close()
