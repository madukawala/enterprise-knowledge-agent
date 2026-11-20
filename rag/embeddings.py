"""Dense vectors from Pinecone's hosted embedding model (ADR-0004).

`multilingual-e5-large` is asymmetric: stored text is embedded as a
"passage" and questions as a "query". Using the wrong type for either side
makes search noticeably worse, so each side has its own class:

- `PineconeEmbedder` embeds document chunks during ingestion (an offline
  job, so it uses the normal blocking client).
- `QueryEmbedder` embeds questions at search time. It is async, so the API
  can serve other requests while it waits for Pinecone.

The Pinecone client already retries rate-limit (429) and server errors with
back-off, so this module only splits the work into batches.
"""

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

# The most inputs Pinecone accepts in one request for this model.
MAX_BATCH_SIZE = 96

# truncate=END: a text that is longer than the model's limit is cut at the
# end instead of failing the whole request. The chunker keeps chunks under
# the limit, and questions are short, so this is only a safety net.
_TRUNCATE = "END"


class InferenceClient(Protocol):
    """The part of `Pinecone().inference` used here. Tests pass a fake."""

    def embed(
        self, model: str, inputs: Sequence[str], parameters: Mapping[str, Any] | None = None
    ) -> Any: ...


class AsyncInferenceClient(Protocol):
    """The part of `AsyncPinecone().inference` used for questions."""

    async def embed(
        self, model: str, inputs: Sequence[str], parameters: Mapping[str, Any] | None = None
    ) -> Any: ...


def _check_vectors(result: Any, *, expected: int, model: str, dimension: int) -> list[list[float]]:
    """Turn the API result into plain lists and check the count and size."""
    vectors = [list(item.values) for item in result]
    if len(vectors) != expected:
        raise ValueError(f"Asked for {expected} embeddings but received {len(vectors)}.")
    for vector in vectors:
        if len(vector) != dimension:
            raise ValueError(
                f"The model {model} returned {len(vector)} dimensions, "
                f"but EMBEDDING_DIMENSION is {dimension}."
            )
    return vectors


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
            result = self._inference.embed(
                model=self.model,
                inputs=batch,
                parameters={"input_type": "passage", "truncate": _TRUNCATE},
            )
            vectors.extend(
                _check_vectors(
                    result, expected=len(batch), model=self.model, dimension=self.dimension
                )
            )
        return vectors


class QueryEmbedder:
    """Embeds questions, keeping the most recent ones in memory.

    The same question is often asked again (a retry, a follow-up that
    repeats the search, or the evaluation trying several alpha values), and
    each embedding call uses free-plan quota. A small cache in memory avoids
    that. It holds vectors only, never answers, so it cannot leak documents
    between users.
    """

    def __init__(
        self,
        inference: AsyncInferenceClient,
        *,
        model: str,
        dimension: int,
        cache_size: int = 256,
    ) -> None:
        self._inference = inference
        self.model = model
        self.dimension = dimension
        self._cache_size = cache_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    async def embed_query(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            self._cache.move_to_end(text)
            return list(cached)

        result = await self._inference.embed(
            model=self.model,
            inputs=[text],
            parameters={"input_type": "query", "truncate": _TRUNCATE},
        )
        (vector,) = _check_vectors(result, expected=1, model=self.model, dimension=self.dimension)

        if self._cache_size > 0:
            self._cache[text] = vector
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return list(vector)
