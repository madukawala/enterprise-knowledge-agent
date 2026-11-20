"""Quality evaluations. Run on purpose (they use API quota), never by the app.

- `metrics`: hit rate, MRR and recall for retrieval.
- `dataset`: loads and checks the question sets in `evals/datasets/`.
- `retrieval`: runs the retrieval set against the real index and compares
  alpha values (`make eval-retrieval`).

Answer-quality evaluations (Ragas, LangSmith) are added by the
observability branch. See evals/README.md.
"""
