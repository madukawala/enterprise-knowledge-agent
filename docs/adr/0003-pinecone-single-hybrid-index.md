# ADR-0003: One Pinecone hybrid index with weighted dense and sparse scores

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The assignment requires hybrid search (dense semantic search plus sparse BM25 keyword search) on Pinecone, "combining dense score and sparse score", with namespaces, metadata filtering and document attribution. Banking questions mix meaning ("why do card payments fail?") with exact terms (incident IDs, error codes, system names), so both kinds of search are needed.

## Decision

- **One serverless index** with the **`dotproduct`** metric. Each record holds a dense vector **and** a sparse vector, plus metadata.
- At query time the dense query vector is multiplied by `alpha` and the sparse query vector by `1 − alpha`. Because the dot product is linear, the score Pinecone returns is:

  ```text
  score = alpha × dense_score + (1 − alpha) × sparse_score
  ```

- `alpha` is set in config (default `0.5`) and tuned with the evaluation set (hit rate, MRR).
- Metadata filters (`access_level`, `department`, `document_type`, `created_ts`) apply to both parts in the same query.
- The pipeline is shown in [04-search-pipeline](../architecture/04-search-pipeline.md).

## Consequences

- **Positive:** exactly the score combination the assignment describes, in one round trip per namespace.
- **Positive:** permission filters are applied inside Pinecone, so restricted chunks never leave the database for users who may not see them.
- **Positive:** one index fits the free Starter plan.
- **Negative:** dense and sparse scores have different scales, so `alpha` has to be tuned rather than assumed. The evaluation set makes this measurable.
- **Negative:** the BM25 vocabulary is fixed when the encoder is fitted. New documents need a re-fit and re-index (`make ingest`, an Admin tool).

## Alternatives considered

| Option | Why not |
|---|---|
| Two indexes (dense and sparse) merged with Reciprocal Rank Fusion | Fuses ranks, not scores (not what the assignment asks); twice the queries; filters applied twice |
| Pinecone's integrated sparse model only | Not BM25, which the assignment names |
| Dense search with a local BM25 library on the side | Keyword results would bypass Pinecone's metadata filters and attribution |

## Related

[ADR-0004](0004-embeddings-and-bm25.md) · [ADR-0005](0005-pinecone-namespaces.md) · [ADR-0010](0010-reranking.md)
