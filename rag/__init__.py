"""Document search, shared by ingestion and retrieval.

- `schema`: collections (Pinecone namespaces), document types and the chunk
  metadata stored with every record.
- `bm25`: the keyword (sparse) encoder, fitted on our own documents.
- `embeddings`: dense vectors from Pinecone's hosted embedding model.
- `vector_store`: the Pinecone hybrid index, as ingestion writes it.
- `catalog`: a summary of what is indexed, used by research to explore the
  collections without loading documents.
- `filters`, `hybrid`, `reranker`, `retrieval`: search itself, from a
  question and a role to ranked chunks with citations.
- `telemetry`: Prometheus metrics for search.
- `cli`: `make search`, for trying searches from the terminal.

See rag/README.md for how the pieces fit together.
"""
