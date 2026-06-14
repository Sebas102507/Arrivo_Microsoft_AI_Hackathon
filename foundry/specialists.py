"""Specialist agent definitions for the Arrivo multi-agent team.

Each specialist is a Foundry agent grounded on ONE slice of the `arrivo-kb`
knowledge base (via an Azure AI Search `category` filter) with a focused remit.
A single orchestrator (see ``team.py``) connects to them with the "agent as a
tool" pattern and routes each user question to the right specialist(s).

`tool_name` must be a valid function identifier (letters, digits, underscores) —
it is what the orchestrator "calls". `category` matches the index field written
by the ingestion pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass

# Shared rules every specialist must obey, appended to each one's instructions.
_COMMON = """
Answer ONLY from your Azure AI Search knowledge tool, and cite the source for
every factual claim. If the knowledge base lacks the answer, reply with exactly
`INSUFFICIENT_EVIDENCE` plus one sentence naming the official source to check.
Never give migration/visa advice (that legally requires a registered MARA agent);
if asked, say so and refer to https://www.mara.gov.au. Be concise and practical,
and flag any figures that are indicative rather than official.
"""


@dataclass(frozen=True)
class SpecialistSpec:
    key: str            # short id used in env + filenames
    tool_name: str      # how the orchestrator invokes it (function-style name)
    category: str       # arrivo-kb category filter (empty = whole index)
    description: str     # tells the orchestrator WHEN to use this specialist
    instructions: str    # the specialist's own system prompt (minus _COMMON)

    @property
    def name(self) -> str:
        return f"arrivo-{self.key}-specialist"

    @property
    def full_instructions(self) -> str:
        return self.instructions.strip() + "\n" + _COMMON.strip()


SPECIALISTS: list[SpecialistSpec] = [
    SpecialistSpec(
        key="entitlements",
        tool_name="entitlements_specialist",
        category="entitlements",
        description=(
            "Health and welfare entitlements for new arrivals: Medicare enrolment and "
            "eligibility, Centrelink payments, and the Newly Arrived Resident's Waiting "
            "Period. Use for questions about healthcare access and government payments."
        ),
        instructions=(
            "You are Arrivo's entitlements specialist. You cover Medicare enrolment and "
            "eligibility by visa type, Centrelink/Services Australia payments, and newly "
            "arrived resident waiting periods for migrants settling in Australia."
        ),
    ),
    SpecialistSpec(
        key="first-steps",
        tool_name="first_steps_specialist",
        category="first_steps",
        description=(
            "The earliest admin tasks: applying for a Tax File Number (TFN) and opening "
            "an Australian bank account. Use for questions about TFNs, tax registration, "
            "and banking setup for newcomers."
        ),
        instructions=(
            "You are Arrivo's first-steps specialist. You cover applying for a Tax File "
            "Number (including for foreign passport holders) and opening an Australian "
            "bank account, plus what documents are needed and in what order."
        ),
    ),
    SpecialistSpec(
        key="housing",
        tool_name="housing_specialist",
        category="housing",
        description=(
            "Renting a home in NSW: tenancy rights, rental applications without local "
            "history, rental bonds, and starting a tenancy. Use for questions about "
            "leases, bonds, and what landlords/agents require."
        ),
        instructions=(
            "You are Arrivo's housing specialist. You cover renting in NSW: tenant rights, "
            "what a newcomer with no Australian rental history can provide in an "
            "application, rental bond rules, and starting a tenancy."
        ),
    ),
    SpecialistSpec(
        key="transport",
        tool_name="transport_specialist",
        category="transport",
        description=(
            "Getting around NSW: converting an overseas driver licence and using public "
            "transport / the Opal card. Use for questions about driving and transit."
        ),
        instructions=(
            "You are Arrivo's transport specialist. You cover converting an overseas "
            "driver licence in NSW (including deadlines) and using public transport and "
            "the Opal card."
        ),
    ),
    SpecialistSpec(
        key="education",
        tool_name="education_specialist",
        category="education",
        description=(
            "Enrolling children in NSW public schools, including rules for temporary "
            "residents and visa holders. Use for questions about school enrolment."
        ),
        instructions=(
            "You are Arrivo's education specialist. You cover enrolling a child in a NSW "
            "public school, including requirements for temporary residents and visa "
            "holders."
        ),
    ),
]


def specialist_by_key(key: str) -> SpecialistSpec:
    for s in SPECIALISTS:
        if s.key == key:
            return s
    raise KeyError(key)
