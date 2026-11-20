"""Dense vectors from Pinecone's hosted embedding model (ADR-0004).

`multilingual-e5-large` is asymmetric: stored text is embedded as a
"passage" and questions as a "query". Using the wrong type for either side
makes search noticeably worse, so each side has its own method.

The Pinecone client already retries rate-limit (429) and server errors with
back-off, so this module only splits the work into batches.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

# The most inputs Pinecone accepts in one request for this model.
MAX_BATCH_SIZE = 96


class InferenceClient(Protocol):
    """The part of `Pinecone().inference` used here. Tests pass a fake."""

    def embed(
        self, model: str, inputs: Sequence[str], parameters: Mapping[str, Any] | None = None
    ) -> Any: ...


class PineconeEmbedder:
    def __init__(
        self,
        inference: InferenceClient,
        *,
        model: str,
        dimension: int,
        batch_size: int = MAX_BATCH_SIZE,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
        self._inference = inference
        self.model = model
        self.dimension = dimension
        self.batch_size = batch_size

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed text that will be stored and searched (document chunks)."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            vectors.extend(self._embed(batch, input_type="passage"))
        return vectors

    def _embed(self, batch: list[str], *, input_type: str) -> list[list[float]]:
        # truncate=END: a chunk that is longer than the model's limit is cut
        # at the end instead of failing the whole batch. The chunker keeps
        # chunks under the limit, so this is only a safety net.
        result = self._inference.embed(
            model=self.model,
            inputs=batch,
            parameters={"input_type": input_type, "truncate": "END"},
        )
        vectors = [list(item.values) for item in result]
        if len(vectors) != len(batch):
            raise ValueError(f"Asked for {len(batch)} embeddings but received {len(vectors)}.")
        for vector in vectors:
            if len(vector) != self.dimension:
                raise ValueError(
                    f"The model {self.model} returned {len(vector)} dimensions, "
                    f"but EMBEDDING_DIMENSION is {self.dimension}."
                )
        return vectors
