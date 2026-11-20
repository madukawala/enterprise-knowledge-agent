"""Offline job that loads the sample documents into Pinecone (`make ingest`).

Steps: read and check each Markdown file -> split it into chunks -> fit the
BM25 encoder -> build dense and sparse vectors -> upsert into one namespace
per collection -> remove records of deleted documents -> save the BM25
parameters and the catalog.

See ingestion/README.md for details and how to run it.
"""
