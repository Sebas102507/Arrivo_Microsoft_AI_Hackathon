"""Arrivo knowledge-base ingestion.

Builds the grounding layer (Foundry IQ pattern): an Azure AI Search index over
public Australian government settlement documents, with vector + keyword search.

Usage:
    pip install -r ../backend/requirements.txt
    python ingest.py            # fetch sources.json URLs + ingest local ./docs files
    python ingest.py --local    # skip URL fetching, only ingest ./docs
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import AzureOpenAI
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration, SearchableField, SearchField,
    SearchFieldDataType, SearchIndex, SimpleField, VectorSearch,
    VectorSearchProfile,
)

load_dotenv(Path(__file__).parent.parent / "backend" / ".env")

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_KEY = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "arrivo-kb")
EMBED_DEPLOYMENT = os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"]
EMBED_DIMS = 1536  # text-embedding-3-small

aoai = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-10-21",
)


def create_index() -> None:
    client = SearchIndexClient(SEARCH_ENDPOINT, AzureKeyCredential(SEARCH_KEY))
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SimpleField(name="source_url", type=SearchFieldDataType.String),
        SimpleField(name="category", type=SearchFieldDataType.String,
                    filterable=True, facetable=True),
        SearchField(name="vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, vector_search_dimensions=EMBED_DIMS,
                    vector_search_profile_name="default"),
    ]
    vs = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
        profiles=[VectorSearchProfile(name="default", algorithm_configuration_name="hnsw")],
    )
    client.create_or_update_index(SearchIndex(name=INDEX_NAME, fields=fields, vector_search=vs))
    print(f"Index '{INDEX_NAME}' ready.")


def fetch_url(url: str) -> str:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "ArrivoIngest/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
    return text.strip()


def chunk(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    paras, chunks, cur = text.split("\n\n"), [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            chunks.append(cur.strip())
            cur = cur[-overlap:] + "\n\n" + p
        else:
            cur += "\n\n" + p
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if len(c) > 100]


def embed(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), 16):
        resp = aoai.embeddings.create(model=EMBED_DEPLOYMENT, input=texts[i:i + 16])
        out.extend(d.embedding for d in resp.data)
        time.sleep(0.2)
    return out


def upload(docs: list[dict]) -> None:
    client = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, AzureKeyCredential(SEARCH_KEY))
    for i in range(0, len(docs), 100):
        client.upload_documents(docs[i:i + 100])
    print(f"Uploaded {len(docs)} chunks.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="skip URL fetching")
    args = parser.parse_args()

    create_index()
    sources = json.loads((Path(__file__).parent / "sources.json").read_text())
    all_docs: list[dict] = []

    if not args.local:
        for src in sources:
            try:
                print(f"Fetching {src['url']}")
                text = fetch_url(src["url"])
            except Exception as exc:  # noqa: BLE001 — keep going, report at end
                print(f"  !! failed ({exc}) — save the page manually into ingestion/docs/ as .txt")
                continue
            pieces = chunk(text)
            vectors = embed(pieces)
            for j, (piece, vec) in enumerate(zip(pieces, vectors)):
                uid = hashlib.md5(f"{src['url']}#{j}".encode()).hexdigest()
                all_docs.append({
                    "id": uid, "content": piece, "title": src["title"],
                    "source_url": src["url"], "category": src["category"], "vector": vec,
                })

    # Local .txt files in ingestion/docs/ — name them "<category>__<title>.txt"
    for path in sorted((Path(__file__).parent / "docs").glob("*.txt")):
        category, _, title = path.stem.partition("__")
        pieces = chunk(path.read_text())
        vectors = embed(pieces)
        for j, (piece, vec) in enumerate(zip(pieces, vectors)):
            uid = hashlib.md5(f"{path.name}#{j}".encode()).hexdigest()
            all_docs.append({
                "id": uid, "content": piece, "title": title or path.stem,
                "source_url": f"local://{path.name}", "category": category or "general",
                "vector": vec,
            })

    if not all_docs:
        sys.exit("No documents ingested — check sources.json or add .txt files to ingestion/docs/")
    upload(all_docs)


if __name__ == "__main__":
    main()
