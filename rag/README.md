# rag

Building blocks for document search. Ingestion uses them to fill the index, and retrieval (`feature/hybrid-retrieval`) will use the same ones to search it. Keeping them in one place means both sides agree on IDs, metadata and encoders.

| Module | What it does |
|---|---|
| [schema.py](schema.py) | Collections (one Pinecone namespace each), document types, departments, record IDs (`<doc_id>#<chunk_index>`) and `ChunkMetadata`, the metadata stored with every vector |
| [bm25.py](bm25.py) | The BM25 keyword encoder: fitted on our chunks, builds sparse vectors for documents and questions, saved to and loaded from JSON |
| [embeddings.py](embeddings.py) | Dense vectors from Pinecone's hosted `multilingual-e5-large`, in batches |
| [vector_store.py](vector_store.py) | The Pinecone hybrid index: create it (`dotproduct`, 1024 dimensions), upsert, list and delete records |
| [catalog.py](catalog.py) | The catalog model: what each collection holds, per access level |

## How the hybrid score works

Each record stores a dense vector and a BM25 sparse vector. At query time retrieval:

1. scales the dense query vector by `alpha`;
2. scales the sparse query vector by `1 - alpha`;
3. sends both in one query.

Because the index uses the dot product, Pinecone returns:

```text
score = alpha × dense score + (1 − alpha) × BM25 score
```

That is the weighted combination the assignment asks for, in one query per namespace. See [ADR-0003](../docs/adr/0003-pinecone-single-hybrid-index.md) and [04-search-pipeline](../docs/architecture/04-search-pipeline.md).

## Why our own BM25 encoder

The plan was to use `pinecone-text`'s `BM25Encoder`. Its tokenizer downloads NLTK data the first time it runs, which fails in the read-only API container and pulls a large library into the image. [bm25.py](bm25.py) uses the same formula in about 200 lines, comments included:

- a simple tokenizer that keeps codes like `INC-2026-003` and `payhub-db`, and their parts;
- a short stop-word list;
- terms hashed to fixed 32-bit positions, so a term keeps its position after a re-fit.

The change is recorded in [ADR-0004](../docs/adr/0004-embeddings-and-bm25.md).

## Access levels

Every chunk carries its document's `access_level`. Retrieval will add a filter on it to every query, built from the user's role in [security/roles.py](../security/roles.py), so chunks above a user's level never leave Pinecone. The catalog keeps its counts per access level for the same reason: a Viewer is never told that restricted documents exist.
