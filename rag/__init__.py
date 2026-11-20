"""Building blocks for document search, shared by ingestion and retrieval.

- `schema`: collections (Pinecone namespaces), document types and the chunk
  metadata stored with every record.
- `bm25`: the keyword (sparse) encoder, fitted on our own documents.
- `embeddings`: dense vectors from Pinecone's hosted embedding model.
- `vector_store`: the Pinecone hybrid index.
- `catalog`: a summary of what is indexed, used by research to explore the
  collections without loading documents.

See rag/README.md for how the pieces fit together.
"""
