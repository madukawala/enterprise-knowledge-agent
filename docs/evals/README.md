# Evaluation results

Results of the evaluations in [evals/](../../evals/README.md), saved here so reviewers can see them without running anything. Each report states its date, the models used and the settings.

| Report | Written by | Measures |
|---|---|---|
| `retrieval.md` | `make eval-retrieval` | Hit rate, MRR and recall of hybrid search, for several alpha values |
| Answer quality (Ragas, LangSmith) | `make eval`, added with `feature/observability-evals` | Faithfulness, relevancy, citation accuracy, routing accuracy |

## Status

No results have been recorded yet. `make eval-retrieval` runs against the real Pinecone index, so it needs `PINECONE_API_KEY` and one `make ingest` first. The first report will be saved here as `retrieval.md`, and `HYBRID_ALPHA` will be set to the alpha it recommends.

Until then, `HYBRID_ALPHA` stays at `0.5`, an equal weight for meaning and keywords. This is recorded as an assumption in [ASSUMPTIONS.md](../ASSUMPTIONS.md).
