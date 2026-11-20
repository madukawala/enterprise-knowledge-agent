# ADR-0010: Hosted `bge-reranker-v2-m3`, skipped on failure

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

Re-ranking is a bonus point. Hybrid search finds good candidates, but a cross-encoder orders them better for the final prompt. The re-ranker must be free and must not run on the laptop.

## Decision

- Use Pinecone's hosted **`bge-reranker-v2-m3`** through the Inference API on the merged hybrid results (top-k ≈ 30 → top-n ≈ 8, set in config).
- If the call fails, times out or the free quota is used up, **skip it and keep the hybrid order**. The activity panel shows "reranker skipped" and a metric counts it.

## Consequences

- **Positive:** better context precision with no local model and no new API key.
- **Positive:** a re-ranker failure never fails a request (graceful degradation).
- **Negative:** one more network call per question (a few hundred milliseconds). It runs once per question, after the parallel namespace queries.

## Alternatives considered

| Option | Why not |
|---|---|
| Local cross-encoder | RAM and CPU on the laptop |
| LLM-based re-ranking | Slow and uses the LLM quota |
| No re-ranking | Loses the bonus point and some answer quality |

## Related

[04-search-pipeline](../architecture/04-search-pipeline.md) · [07-failure-handling](../architecture/07-failure-handling.drawio.svg)
