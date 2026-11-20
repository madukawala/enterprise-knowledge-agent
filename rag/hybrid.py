"""The weighted combination of the dense and the BM25 score (ADR-0003).

The index uses the dot product, which is linear. So if we scale the query
vectors before sending them:

    dense query  x alpha
    sparse query x (1 - alpha)

then the score Pinecone returns for every record is

    alpha x (dense_q . dense_d) + (1 - alpha) x (sparse_q . sparse_d)
    = alpha x dense score + (1 - alpha) x BM25 score

That is the whole "score merge": one query per namespace, with the
permission and metadata filters applied to both parts at once.
"""

from collections.abc import Sequence

from rag.bm25 import SparseVector


def hybrid_query_vectors(
    dense: Sequence[float], sparse: SparseVector, alpha: float
) -> tuple[list[float], SparseVector | None]:
    """Scale the two query vectors by `alpha`.

    Returns `None` for the sparse part when it would add nothing: at
    alpha=1, or when the question has no searchable words (only stop
    words). Pinecone does not accept an empty sparse vector, so sending
    nothing is also the only valid choice.
    """
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be greater than 0 and at most 1")

    scaled_dense = [value * alpha for value in dense]
    if alpha == 1 or sparse.is_empty():
        return scaled_dense, None
    scaled_sparse = SparseVector(
        indices=list(sparse.indices),
        values=[value * (1 - alpha) for value in sparse.values],
    )
    return scaled_dense, scaled_sparse
