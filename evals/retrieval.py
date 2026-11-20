"""Measure retrieval quality and compare alpha values.

    make eval-retrieval
    uv run python -m evals.retrieval --alphas 0.3,0.5,0.7 --k 5 --report docs/evals/retrieval.md

Runs every question in evals/datasets/retrieval.yaml against the real
Pinecone index (so `make ingest` must have run), once per alpha, and prints
hit rate, MRR and recall. The best alpha is the one to put in HYBRID_ALPHA.

Cost: one embedding call per question (the vector is cached across alpha
values) and one query per collection per question per alpha. The re-ranker
is off by default, so the numbers show the hybrid search itself; add
--rerank to measure the full pipeline.

Exit codes: 0 done, 1 search failed, 2 missing settings or `make ingest` not run.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from core.config import LogFormat, Settings, get_settings
from core.errors import RetrievalError
from core.logging import configure_logging
from evals.dataset import RETRIEVAL_SET, RetrievalCase, RetrievalSet, load_retrieval_set
from evals.metrics import Scores, score_all
from rag.retrieval import HybridRetriever, RetrievalSetupError, open_retriever

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_CONFIGURED = 2


@dataclass(frozen=True)
class CaseOutcome:
    case: RetrievalCase
    ranked: list[str]

    @property
    def first_hit_rank(self) -> int | None:
        for position, doc_id in enumerate(self.ranked, start=1):
            if doc_id in self.case.expected:
                return position
        return None


@dataclass(frozen=True)
class AlphaRun:
    alpha: float
    scores: Scores
    outcomes: list[CaseOutcome]


async def evaluate(
    retriever: HybridRetriever, dataset: RetrievalSet, *, alpha: float, k: int
) -> AlphaRun:
    """Run every question once with this alpha.

    Questions run one after another, not all at once, to stay well inside
    the free plan's request rate.
    """
    outcomes: list[CaseOutcome] = []
    for case in dataset.cases:
        result = await retriever.search(case.request(alpha=alpha, top_k=k), role=case.role)
        outcomes.append(CaseOutcome(case=case, ranked=result.doc_ids()))
    scores = score_all([(o.ranked, o.case.expected) for o in outcomes], k=k)
    return AlphaRun(alpha=alpha, scores=scores, outcomes=outcomes)


def best_run(runs: Sequence[AlphaRun]) -> AlphaRun:
    """Highest MRR; on a tie the higher hit rate, then the alpha nearer 0.5."""
    return max(runs, key=lambda r: (r.scores.mrr, r.scores.hit_rate, -abs(r.alpha - 0.5)))


def render_report(
    runs: Sequence[AlphaRun], *, k: int, rerank: bool, settings: Settings, today: date
) -> str:
    best = best_run(runs)
    lines = [
        "# Retrieval evaluation",
        "",
        f"- Date: {today.isoformat()}",
        f"- Questions: {best.scores.questions} (`evals/datasets/retrieval.yaml`)",
        f"- Embedding model: `{settings.embedding_model}`",
        f"- Re-ranker: {f'`{settings.rerank_model}`' if rerank else 'off (hybrid order only)'}",
        f"- k = {k}, metrics at document level",
        "",
        f"| alpha | hit@{k} | MRR | recall@{k} |",
        "|---|---|---|---|",
    ]
    for run in runs:
        marker = " (best)" if run is best else ""
        lines.append(
            f"| {run.alpha}{marker} | {run.scores.hit_rate:.2f} | {run.scores.mrr:.2f} "
            f"| {run.scores.recall:.2f} |"
        )
    lines += [
        "",
        f"## Per question, alpha {best.alpha}",
        "",
        "| Question | Role | Expected | First expected at | Returned (top 3) |",
        "|---|---|---|---|---|",
    ]
    for outcome in best.outcomes:
        rank = outcome.first_hit_rank
        lines.append(
            f"| {outcome.case.id} | {outcome.case.role.value} "
            f"| {', '.join(outcome.case.expected)} | {rank if rank else 'not found'} "
            f"| {', '.join(outcome.ranked[:3]) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.retrieval")
    parser.add_argument(
        "--alphas",
        default="0.3,0.5,0.7,0.9",
        type=lambda text: [float(part) for part in text.split(",")],
        help="comma-separated alpha values to compare (default: 0.3,0.5,0.7,0.9)",
    )
    parser.add_argument("--k", type=int, default=5, choices=range(1, 21), metavar="1-20")
    parser.add_argument("--rerank", action="store_true", help="include the re-ranker")
    parser.add_argument("--dataset", type=Path, default=RETRIEVAL_SET)
    parser.add_argument("--report", type=Path, help="also save the report as Markdown here")
    return parser.parse_args(argv)


async def _run_all(
    settings: Settings, dataset: RetrievalSet, alphas: Sequence[float], k: int
) -> list[AlphaRun]:
    async with open_retriever(settings) as retriever:
        return [await evaluate(retriever, dataset, alpha=alpha, k=k) for alpha in alphas]


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = _parse_args(argv)
    settings = (settings or get_settings()).model_copy(update={"rerank_enabled": args.rerank})
    configure_logging("WARNING", LogFormat.CONSOLE)
    if any(not 0 < alpha <= 1 for alpha in args.alphas):
        print("Every alpha must be greater than 0 and at most 1.", file=sys.stderr)
        return EXIT_FAILED

    dataset = load_retrieval_set(args.dataset)
    try:
        runs = asyncio.run(_run_all(settings, dataset, args.alphas, args.k))
    except RetrievalSetupError as error:
        print(str(error), file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    except RetrievalError as error:
        print(f"{error.message} Check the warnings above.", file=sys.stderr)
        return EXIT_FAILED

    report = render_report(
        runs, k=args.k, rerank=args.rerank, settings=settings, today=date.today()
    )
    print(report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"Saved {args.report}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
