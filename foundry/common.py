"""Shared helpers for the Arrivo Foundry agents (answer types, citations, query types)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from azure.ai.agents.models import AzureAISearchQueryType

QUERY_TYPES = {
    "simple": AzureAISearchQueryType.SIMPLE,
    "semantic": AzureAISearchQueryType.SEMANTIC,
    "vector": AzureAISearchQueryType.VECTOR,
    "vector_simple_hybrid": AzureAISearchQueryType.VECTOR_SIMPLE_HYBRID,
    "vector_semantic_hybrid": AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID,
}


@dataclass
class AgentAnswer:
    answer: str
    citations: list[dict]  # [{"title": ..., "url": ...}]
    run_status: str


@dataclass
class ReflectionResult:
    """Final answer plus the evaluator verdict and the reflection trace."""
    answer: str
    citations: list[dict]
    passed: bool
    evaluation: dict
    revisions: int
    trace: list[dict]


def _load_title_to_url() -> dict[str, str]:
    """Map knowledge-base titles → real source URLs.

    The Azure AI Search agent tool surfaces citations with the document title but
    often a placeholder or the search-service URL (the SDK has no URL field mapping).
    We restore real, clickable gov links by title using the index's source list.
    """
    src = Path(__file__).resolve().parent.parent / "ingestion" / "sources.json"
    try:
        return {s["title"]: s["url"] for s in json.loads(src.read_text())}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


TITLE_TO_URL = _load_title_to_url()

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"(?<!\()(?<!\])\bhttps?://[^\s)\]]+")


def extract_inline_citations(text: str) -> list[dict]:
    """Recover citations from answer text when no structured annotations are present."""
    found: list[dict] = []
    seen: set[str] = set()
    for label, url in _MD_LINK.findall(text):
        if url not in seen:
            seen.add(url)
            found.append({"title": label.strip(), "url": url})
    for url in _BARE_URL.findall(text):
        clean = url.rstrip(".,);")
        if clean not in seen:
            seen.add(clean)
            found.append({"title": clean.split("//", 1)[-1].split("/", 1)[0], "url": clean})
    return found
