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

## Update during implementation (feature/sample-data-ingestion)

- **BM25 is our own small encoder (`rag/bm25.py`), not `pinecone-text`.** `pinecone-text`'s tokenizer downloads NLTK data the first time it runs. That fails in the read-only API container, and it would add NLTK and NumPy to the API image. Our encoder uses the same formula (document term weights with `k1` and `b`, query weights from idf, scaled to add up to 1), so the dot product is still the BM25 score.
- Tokens are lower-case words and codes such as `inc-2026-003` and `payhub-db`, plus the parts of each code. There is a short stop-word list and no stemming, because stemming would mangle codes and the dense vector already handles word forms.
- Terms are hashed to fixed 32-bit positions, so a term keeps its position after a re-fit.
- **Token counts are estimated.** The e5 tokenizer is not installed. Words and punctuation marks are counted and a third is added, which errs on the high side. The embedding call uses `truncate=END` as a safety net.
- **Chunks are embedded with their title and section in front** ("Title - Section"), so short chunks still carry their context. Only the chunk text is stored for display.
- **No separate retry library.** The Pinecone client already retries 429 and 5xx responses with back-off.

## Alternatives considered

| Option | Why not |
|---|---|
| Gemini embeddings | Uses the same free quota as reasoning |
| Local `sentence-transformers` model | Uses laptop RAM and CPU inside the API container |
| Default (pre-fitted) BM25 parameters | Trained on general web text, not our vocabulary |
| `pinecone-text` `BM25Encoder` | Downloads NLTK data at runtime (fails in a read-only container) and adds heavy dependencies; replaced by the same formula in `rag/bm25.py` |
| SPLADE / learned sparse models | Not BM25, which the assignment names |

## Related

[04-search-pipeline](../architecture/04-search-pipeline.md)
