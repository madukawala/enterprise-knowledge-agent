# rag

Everything to do with document search. Ingestion uses these modules to fill the index, and retrieval uses the same ones to search it. Keeping them in one place means both sides agree on IDs, metadata and encoders.

| Module | What it does |
|---|---|
| [schema.py](schema.py) | Collections (one Pinecone namespace each), document types, departments, record IDs (`<doc_id>#<chunk_index>`) and `ChunkMetadata`, the metadata stored with every vector |
| [bm25.py](bm25.py) | The BM25 keyword encoder: fitted on our chunks, builds sparse vectors for documents and questions, saved to and loaded from JSON |
| [embeddings.py](embeddings.py) | Dense vectors from Pinecone's hosted `multilingual-e5-large`: passages in batches for ingestion, questions (async, cached) for search |
| [vector_store.py](vector_store.py) | The Pinecone hybrid index for ingestion: create it (`dotproduct`, 1024 dimensions), upsert, list and delete records |
| [catalog.py](catalog.py) | The catalog model: what each collection holds, per access level |
| [filters.py](filters.py) | The Pinecone filter for a search: the access-level rule for the user's role, plus optional department, type, severity, date and document filters |
| [hybrid.py](hybrid.py) | Weights the dense and BM25 query vectors by `alpha` |
| [reranker.py](reranker.py) | Pinecone's hosted `bge-reranker-v2-m3` |
| [retrieval.py](retrieval.py) | `HybridRetriever`: one search from question to cited chunks, and `open_retriever()` to build one from the settings |
| [telemetry.py](telemetry.py) | Prometheus metrics for search |
| [cli.py](cli.py) | `make search`: try a search from the terminal |

## How a search works

```python
async with open_retriever(settings) as retriever:
    result = await retriever.search(
        SearchRequest(query="why do card payments fail?", collections=(Collection.INCIDENTS,)),
        role=Role.ANALYST,  # from the login token, never from the request
    )
for hit in result.hits:
    print(hit.score, hit.citation.label)
```

1. **Filter.** [filters.py](filters.py) builds one Pinecone filter. It always starts with `access_level in <the role's levels>`, then adds the optional filters with `$and`.
2. **Encode.** The question is embedded as a "query" (e5 is asymmetric), and encoded with the BM25 parameters that `make ingest` saved. Recent question vectors are cached in memory, which saves quota on retries and evaluation runs.
3. **Weight.** The dense vector is multiplied by `alpha` and the BM25 vector by `1 - alpha` (see below).
4. **Query in parallel.** One query per collection, all at the same time with `asyncio.gather`. Each one has its own time limit.
5. **Merge.** All matches are sorted by score and de-duplicated, with at most 3 chunks per document. One long document cannot push every other source out.
6. **Re-rank.** The best 30 candidates go to the re-ranker, which reads the question and each chunk together and returns the top `k`.
7. **Return.** A `SearchResult` with the hits (each with its chunk, score and citation), how many candidates were found, which collections failed, and whether the re-ranker ran. The activity panel shows these numbers.

## How the hybrid score works

Each record stores a dense vector and a BM25 sparse vector. The index uses the dot product, which is linear. So scaling the query vectors before sending them gives:

```text
score = alpha × dense score + (1 − alpha) × BM25 score
```

That is the weighted combination the assignment asks for, in one query per namespace, with the filters applied to both parts at once. A property test ([tests/unit/test_hybrid.py](../tests/unit/test_hybrid.py)) checks the equation on random vectors. A live test (`make test-live`) checks it against real Pinecone: it asks for the stored vectors back and compares Pinecone's score with the formula. Pinecone Local can't check it, because the emulator ignores the sparse part of a query.

- `alpha` is `HYBRID_ALPHA` (default `0.5`). A request can override it.
- `alpha` must be above 0: at 0 the dense query vector would be all zeros, which Pinecone does not accept. `1.0` means meaning only.
- A question with no searchable words (only stop words) sends no sparse part, because Pinecone rejects an empty sparse vector.
- Choose `alpha` with `make eval-retrieval` (see [evals/README.md](../evals/README.md)).

## When something fails

Search runs while a user waits, so it degrades instead of failing whenever it can:

| What fails | What happens |
|---|---|
| One or more collections time out or error | The others still answer. `result.failed_collections` lists what was missed, so the answer can say so |
| Every collection fails | `RetrievalError` (HTTP 503, "Document search is unavailable right now.") |
| The question cannot be embedded | `RetrievalError`, since no collection can be searched without it |
| The re-ranker fails, times out or runs out of free quota | The hybrid order is kept, and `result.rerank` is `failed` |
| A record has broken metadata (e.g. from an older ingestion) | That record is skipped and counted |
| The search is cancelled (the user left) | The cancellation reaches every running query; nothing is left running |

Every Pinecone call is limited to `RETRIEVAL_TIMEOUT_SECONDS` (default 8). The client also retries less than its default (2 retries, at most 2 seconds apart), because a person is waiting. The circuit breaker and the "reduced mode" banner come with `feature/resilience`.

## Access levels

- The role is a separate argument to `search()`, never a field of `SearchRequest`, and `SearchFilters` has no access-level field. A request cannot widen its own access, and `extra="forbid"` rejects any attempt.
- The filter is applied inside Pinecone, so chunks above the user's level never leave the database.
- As a second line of defence, the retriever checks every returned chunk against the role again. A mismatch can only mean a filter bug. The chunk is dropped, an `access_filter_mismatch` error is logged and `retrieval_dropped_records_total{reason="access_level_mismatch"}` goes up. A security test breaks the filter on purpose to prove this.
- The catalog keeps its counts per access level for the same reason: a Viewer is never told that restricted documents exist.

## Logs and metrics

- One `search_finished` log line per search, with the role, the collections, candidates, hits, alpha, the re-ranker status and the time taken. The question itself is never logged, because it may contain personal data.
- Metrics, created on a registry the caller passes in: `retrieval_searches_total{outcome}`, `retrieval_search_duration_seconds`, `retrieval_collection_errors_total{collection,reason}`, `retrieval_rerank_total{status}` and `retrieval_dropped_records_total{reason}`. The API registers them on `/metrics` when the agents start using search.

## Trying it

```bash
make search Q="why do card payments fail?" ROLE=analyst
uv run python -m rag.cli "salary day failures" --role analyst --collection incidents \
    --department payments --from 2026-01-01 --show-text
uv run python -m rag.cli --help
```

The `--role` option is for local testing. Anyone who can run it already holds the Pinecone key. In the app, the role always comes from the validated login token.

## Why our own BM25 encoder

The plan was to use `pinecone-text`'s `BM25Encoder`. Its tokenizer downloads NLTK data the first time it runs, which fails in the read-only API container and pulls a large library into the image. [bm25.py](bm25.py) uses the same formula in about 200 lines, comments included:

- a simple tokenizer that keeps codes like `INC-2026-003` and `payhub-db`, and their parts;
- a short stop-word list;
- terms hashed to fixed 32-bit positions, so a term keeps its position after a re-fit.

The change is recorded in [ADR-0004](../docs/adr/0004-embeddings-and-bm25.md).

## Not in this package yet

- **Choosing collections and rewriting the question** need the language model, so the retrieval agent does them (`feature/langgraph-agents`). Until then the caller picks the collections; the default is all six.
- **Cleaning retrieved text** (hidden instructions, HTML, zero-width characters) comes with `feature/guardrails`.
