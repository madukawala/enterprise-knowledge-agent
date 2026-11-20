# Evaluation results

Results of the evaluations in [evals/](../../evals/README.md), saved here so reviewers can see them without running anything. Each report states its date, the models used and the settings.

| Report | Written by | Measures |
|---|---|---|
| `retrieval.md` | `make eval-retrieval` | Hit rate, MRR and recall of hybrid search, for several alpha values |
| Answer quality (Ragas, LangSmith) | `make eval`, added with `feature/observability-evals` | Faithfulness, relevancy, citation accuracy, routing accuracy |

## Status

The first retrieval report is in [retrieval.md](retrieval.md) (21 November 2026, 18 questions, re-ranker off, k = 5):

- Every alpha from 0.3 to 0.9 found a good document in the top 5 for all 18 questions (hit@5 = 1.00), with recall@5 of 0.94.
- MRR was 0.93 for 0.3, 0.5 and 0.7, and 0.94 for 0.9: about the size of one question's first good answer moving up one place.

So the search is not sensitive to alpha on this set, and `HYBRID_ALPHA` stays at `0.5`, an equal weight for meaning and keywords. A larger question set, written by real users, would be needed to justify moving it (ASSUMPTIONS R1 and R8).

`make eval-retrieval` runs against the real Pinecone index, so it needs `PINECONE_API_KEY` and one `make ingest` first. Run it again after changing the documents or the search, and commit the new `retrieval.md`.
