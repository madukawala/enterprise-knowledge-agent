# ADR-0004: Hosted `multilingual-e5-large` embeddings and corpus-fitted BM25

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The hybrid index ([ADR-0003](0003-pinecone-single-hybrid-index.md)) needs a dense embedding model and a sparse encoder. Constraints: free, no model running on the laptop, and the Gemini free quota should be kept for reasoning rather than embeddings.

## Decision

- **Dense:** `multilingual-e5-large` (1024 dimensions) through the **Pinecone Inference API**, which is included in the free Starter plan. Documents are embedded with `input_type=passage` and questions with `input_type=query`, as the model expects.
- **Sparse:** true **BM25** with `pinecone-text`'s `BM25Encoder`, **fitted on our own corpus** during ingestion. The fitted parameters are saved with the ingestion output and loaded by the API, so queries use the same vocabulary and document frequencies.
- **Chunking:** split by Markdown headings first, then to about 450 tokens with a small overlap, staying under the model's 512-token input limit. Every chunk keeps its section heading for citations.

## Consequences

- **Positive:** no local model, no extra API key, no use of the LLM quota; multilingual support at no extra cost.
- **Positive:** BM25 statistics come from the bank's own wording (system names, incident codes), which improves keyword matches compared with a generic encoder.
- **Negative:** embedding calls are rate-limited on the free plan. Ingestion batches its requests and retries with back-off; the corpus (60–80 documents) is small.
- **Negative:** adding documents changes BM25 statistics, so the encoder is re-fitted on re-index (the Admin re-index tool does both).

## Alternatives considered

| Option | Why not |
|---|---|
| Gemini embeddings | Uses the same free quota as reasoning |
| Local `sentence-transformers` model | Uses laptop RAM and CPU inside the API container |
| Default (pre-fitted) BM25 parameters | Trained on general web text, not our vocabulary |
| SPLADE / learned sparse models | Not BM25, which the assignment names |

## Related

[04-search-pipeline](../architecture/04-search-pipeline.md)
