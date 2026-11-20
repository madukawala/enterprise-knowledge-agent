"""Command line for ingestion.

    uv run python -m ingestion             # load the documents into Pinecone (make ingest)
    uv run python -m ingestion --dry-run   # only check and chunk them (make ingest-check)

Exit codes: 0 done, 1 bad documents or a Pinecone error, 2 missing settings.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pinecone import Pinecone, PineconeError

from core.config import LogFormat, Settings, get_settings
from core.logging import configure_logging, get_logger
from ingestion.loader import CorpusError
from ingestion.pipeline import (
    IngestReport,
    PreparedCorpus,
    prepare_corpus,
    run_ingestion,
)
from rag.bm25 import PARAMS_FILE as BM25_FILE
from rag.catalog import CATALOG_FILE
from rag.embeddings import PineconeEmbedder
from rag.schema import Collection
from rag.vector_store import IndexConfigError, PineconeHybridStore

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_CONFIGURED = 2

log = get_logger(__name__)


def _parse_args(argv: Sequence[str] | None, settings: Settings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion",
        description="Load the sample documents into the Pinecone hybrid index.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check and chunk the documents only; call no service and write no files",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=settings.sample_docs_dir,
        help=f"documents folder (default: {settings.sample_docs_dir})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=settings.index_artifacts_dir,
        help=f"where to save the BM25 parameters and catalog (default: "
        f"{settings.index_artifacts_dir})",
    )
    return parser.parse_args(argv)


def _print_corpus(corpus: PreparedCorpus) -> None:
    print(f"{'Collection':<16}{'Documents':>10}{'Chunks':>8}  Access levels")
    for collection in Collection:
        summary = corpus.catalog.collections[collection]
        levels = ", ".join(
            f"{level.value} {item.documents}" for level, item in summary.by_access_level.items()
        )
        print(f"{collection.value:<16}{summary.documents:>10}{summary.chunks:>8}  {levels}")
    print(f"{'Total':<16}{len(corpus.documents):>10}{len(corpus.chunks):>8}")


def _print_report(report: IngestReport, out_dir: Path) -> None:
    if report.index_created:
        print("Created the Pinecone index.")
    print(f"{'Namespace':<16}{'Upserted':>10}{'Removed':>9}")
    for namespace, count in report.upserted.items():
        print(f"{namespace:<16}{count:>10}{report.deleted.get(namespace, 0):>9}")
    print(f"Saved {out_dir / BM25_FILE} and {out_dir / CATALOG_FILE}")


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    config = settings or get_settings()
    args = _parse_args(argv, config)
    configure_logging(config.log_level, LogFormat.CONSOLE)

    try:
        corpus = prepare_corpus(args.docs_dir, embedding_model=config.embedding_model)
    except CorpusError as error:
        print(f"Found {len(error.problems)} problem(s) in the documents:", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_FAILED

    _print_corpus(corpus)
    if args.dry_run:
        run_ingestion(corpus, embedder=None, store=None, out_dir=args.out_dir, dry_run=True)
        print("Dry run: the documents are valid. Nothing was sent to Pinecone.")
        return EXIT_OK

    if config.pinecone_api_key is None or not config.pinecone_api_key.get_secret_value():
        print(
            "PINECONE_API_KEY is not set. Add your free key to .env "
            "(see .env.example), or run `make ingest-check` to only check the documents.",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED

    client = Pinecone(api_key=config.pinecone_api_key.get_secret_value())
    embedder = PineconeEmbedder(
        client.inference, model=config.embedding_model, dimension=config.embedding_dimension
    )
    store = PineconeHybridStore(
        client,
        index_name=config.pinecone_index_name,
        dimension=config.embedding_dimension,
        cloud=config.pinecone_cloud,
        region=config.pinecone_region,
    )
    try:
        report = run_ingestion(corpus, embedder=embedder, store=store, out_dir=args.out_dir)
    except IndexConfigError as error:
        print(str(error), file=sys.stderr)
        return EXIT_FAILED
    except PineconeError as error:
        # Only the error type goes to the log. The message is shown to the
        # person running the command, because it usually says what to fix
        # (a wrong key, a quota limit), and never contains the key itself.
        log.error("ingest_failed", error_type=type(error).__name__)
        print(f"Pinecone request failed: {str(error)[:300]}", file=sys.stderr)
        return EXIT_FAILED

    _print_report(report, args.out_dir)
    return EXIT_OK
