"""Re-ranking with Pinecone's hosted `bge-reranker-v2-m3` (ADR-0010).

Hybrid search is good at finding candidates. A re-ranker reads the question
and each candidate together, so it is better at putting the most useful
chunk first. It runs once per question, on the merged results of every
namespace.

This class only makes the call. Deciding what to do when it fails (keep
the hybrid order) is the retriever's job, see rag/retrieval.py.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class AsyncRerankClient(Protocol):
    """The part of `AsyncPinecone().inference` used here."""

    async def rerank(
        self,
        model: str,
        query: str,
        documents: Sequence[Mapping[str, Any]],
        rank_fields: Sequence[str] = ...,
        return_documents: bool = ...,
        top_n: int | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> Any: ...


class PineconeReranker:
    def __init__(self, inference: AsyncRerankClient, *, model: str) -> None:
        self._inference = inference
        self.model = model

    async def rerank(
        self, query: str, texts: Sequence[str], *, top_n: int
    ) -> list[tuple[int, float]]:
        """Return `(position, score)` for the `top_n` best texts, best first.

        Positions refer to the `texts` list that was passed in.
        """
        result = await self._inference.rerank(
            model=self.model,
            query=query,
            documents=[{"text": text} for text in texts],
            rank_fields=["text"],
            # We already have the texts; asking for them back only makes
            # the response bigger.
            return_documents=False,
            top_n=min(top_n, len(texts)),
            # Cut long chunks instead of failing the request. The model reads
            # up to 1024 tokens of question plus chunk.
            parameters={"truncate": "END"},
        )
        ranked = [(int(item.index), float(item.score)) for item in result.data]
        if any(not 0 <= position < len(texts) for position, _ in ranked):
            raise ValueError("The re-ranker returned a position outside the list it was given.")
        return ranked
