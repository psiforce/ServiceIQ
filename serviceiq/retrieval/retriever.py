"""Dense retrieval from Pinecone with tenant isolation and grounding filters applied in the query.

Every search is restricted, inside Pinecone, to:
- the tenant's own namespace (tenant_id comes from authentication, never from the question),
- approval_status == "approved",
- effective_date <= as_of <= expiry_date (expiry is inclusive: an offer ending 2026-11-30 is valid that day),
- access_scope in the caller's allowed scopes (public for website customers).
Filtering inside the vector query, rather than afterwards, means excluded content can never
take up one of the top_k slots or leak into the answer.
"""
from dataclasses import dataclass
from datetime import date

from serviceiq.config import Settings
from serviceiq.ingestion.indexing import Embedder, as_of_number, get_index, openai_embedder

PUBLIC_SCOPES = ("public",)


def build_filter(as_of: date, scopes: tuple[str, ...] = PUBLIC_SCOPES) -> dict:
    today = as_of_number(as_of)
    return {
        "approval_status": {"$eq": "approved"},
        "effective_num": {"$lte": today},
        "expiry_num": {"$gte": today},
        "access_scope": {"$in": list(scopes)},
    }


@dataclass
class RetrievedChunk:
    chunk_id: str
    score: float
    text: str
    metadata: dict

    @property
    def document_id(self) -> str:
        return self.metadata["document_id"]


class Retriever:
    def __init__(self, settings: Settings, index=None, embed: Embedder | None = None):
        self.settings = settings
        self.index = index or get_index(settings)
        self.embed = embed or openai_embedder(settings)
        self.embedding_tokens = 0  # running total, for cost reporting

    def search(self, query: str, tenant_id: str, top_k: int = 10, as_of: date | None = None,
               scopes: tuple[str, ...] = PUBLIC_SCOPES) -> list[RetrievedChunk]:
        vectors, tokens = self.embed([query])
        self.embedding_tokens += tokens
        response = self.index.query(
            vector=vectors[0], top_k=top_k, namespace=tenant_id, include_metadata=True,
            filter=build_filter(as_of or self.settings.serviceiq_as_of_date, scopes),
        )
        return [RetrievedChunk(m.id, m.score, m.metadata.get("text", ""), dict(m.metadata)) for m in response.matches]
