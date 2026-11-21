# evals

Quality measurements that are run by hand, because they call the real models (Claude is paid per token). Nothing in the app imports this folder (an import-linter contract checks it).

| File | What it is |
|---|---|
| [datasets/retrieval.yaml](datasets/retrieval.yaml) | 18 questions, each with the documents that answer it |
| [metrics.py](metrics.py) | Hit rate, MRR and recall |
| [dataset.py](dataset.py) | Loads and checks a question set |
| [retrieval.py](retrieval.py) | Runs the set against the real index, once per alpha |
| [datasets/answers.yaml](datasets/answers.yaml) | 8 questions with what a good answer must do: its kind, the documents to cite (or never cite) and key facts |
| [answers.py](answers.py) | Asks each question through the real agents and scores the answer |

## Answers: kind, citations, key facts and leaks

```bash
make eval-answers                                   # writes docs/evals/answers.md
uv run python -m evals.answers --judge              # also asks a model whether each answer is grounded
uv run python -m evals.answers --langsmith          # also attaches every score to the question's trace
uv run python -m evals.answers --only cut-off-times # one case
```

For each question it measures:

- **right kind of answer**: an answer, a refusal (prompt injection), "out of scope" (investment advice), or "not found". Getting a refusal right matters as much as getting an answer right.
- **citation recall**: the share of the documents the answer should cite that it did cite.
- **mentions**: the share of key words or phrases in the answer, a cheap check that the main fact is there.
- **leaks**: documents the answer cited that are above the role's access. Must always be none.
- **grounded** (with `--judge`): a second model call says whether every claim is supported by the cited sources.

The latest results are in [docs/evals/answers.md](../docs/evals/answers.md).

**Why not Ragas.** Ragas needs several model calls per question for each metric, and its embeddings model, which would multiply the cost of every evaluation run. These measures are simple and cheap, and the grounding judge covers faithfulness when it is wanted.

**With LangSmith** (`--langsmith`, and `LANGSMITH_TRACING=true` with a key): every score is attached to the question's trace as feedback (`eval_kind_correct`, `eval_citation_recall`, `eval_mentions`, `eval_grounded`), next to the users' own thumbs up and down (`user_score`), so quality can be followed over time in one place.

**Cost:** about 3 model calls per question, one more with `--judge`. With `claude-haiku-5-5` a run of the 8 cases costs about one US cent. If Claude fails or is rate limited, some calls move to Gemini; the report says which models ran.

## Retrieval: hit rate and MRR

```bash
make ingest            # once: the index and BM25 parameters must exist
make eval-retrieval    # compares alpha 0.3, 0.5, 0.7 and 0.9 with k = 5
```

It prints a table and saves it to [docs/evals/retrieval.md](../docs/evals/README.md):

- **hit@5**: the share of questions where a good document is in the top 5.
- **MRR**: the average of 1 / (position of the first good document). 1.0 means the first result was always a good one.
- **recall@5**: the share of all good documents that were found. Matters for questions with several sources.

Metrics count documents, not chunks: three chunks of one runbook still count as one document.

The best alpha (highest MRR, then hit rate) is the value to put in `HYBRID_ALPHA`. More options:

```bash
uv run python -m evals.retrieval --alphas 0.4,0.5,0.6 --k 3
uv run python -m evals.retrieval --rerank     # measure with the re-ranker too
```

**Cost:** one embedding per question (the vector is cached, so trying more alphas does not embed again), plus one query per collection per question per alpha. The re-ranker is off by default, so the numbers show the hybrid search itself, which is what alpha changes.

## The question set

The questions are written against the sample documents in `data/sample_docs/` and mix three kinds:

- **the bank's own words and codes** ("INC-2026-003", "PayHub database connection pool"), where BM25 helps;
- **paraphrases** ("keep instant payments going when the scheme is slow" for the FastLane degraded mode), where the dense vector helps;
- **filtered searches** (one collection, a date range), which check the filters do not hide the answer.

Each question is asked as a role that may read every expected document. [tests/unit/test_eval_dataset.py](../tests/unit/test_eval_dataset.py) checks that, and that every expected document exists and passes the question's filters. If a document is renamed or its access level changes, that test fails before the evaluation quietly scores a miss.

To try your own questions without changing the shared set, save them as `datasets/<name>.local.yaml` (git ignores these) and pass `--dataset`.
