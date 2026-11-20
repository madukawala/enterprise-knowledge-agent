"""Try a search from the terminal, before the agents and the UI exist.

    uv run python -m rag.cli "why do card payments fail?" --role analyst
    make search Q="why do card payments fail?" ROLE=analyst

It prints the chunks a role would get, with their scores and citations.
Useful for checking the index after `make ingest` and for seeing how alpha
and the filters change the results.

The --role option is for local testing only. Anyone who can run this
already holds the Pinecone key. In the API, the role always comes from the
validated login token (ADR-0007).

Exit codes: 0 done, 1 search failed, 2 missing settings or `make ingest` not run.
"""

import argparse
import asyncio
import sys
import textwrap
from collections.abc import Sequence
from datetime import date

from pydantic import ValidationError

from core.config import LogFormat, Settings, get_settings
from core.errors import RetrievalError
from core.logging import configure_logging
from rag.filters import SearchFilters
from rag.retrieval import RetrievalSetupError, SearchRequest, SearchResult, open_retriever
from rag.schema import Collection, Department, DocumentType
from security.roles import Role

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_CONFIGURED = 2


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rag.cli", description="Search the Helix Bank documents."
    )
    parser.add_argument("query", help="the question to search for")
    parser.add_argument(
        "--role",
        type=Role,
        choices=list(Role),
        default=Role.VIEWER,
        help="search as this role (default: viewer, the lowest)",
    )
    parser.add_argument(
        "--collection",
        dest="collections",
        type=Collection,
        choices=list(Collection),
        action="append",
        help="search only this collection (repeat for more; default: all)",
    )
    parser.add_argument(
        "--department",
        dest="departments",
        type=Department,
        choices=list(Department),
        action="append",
    )
    parser.add_argument(
        "--type",
        dest="document_types",
        type=DocumentType,
        choices=list(DocumentType),
        action="append",
    )
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--top-k", type=int, help="how many chunks to return")
    parser.add_argument("--alpha", type=float, help="1.0 = meaning only; lower adds more BM25")
    parser.add_argument("--no-rerank", action="store_true", help="keep the hybrid order")
    parser.add_argument("--show-text", action="store_true", help="print each chunk's text")
    return parser.parse_args(argv)


def _print_result(result: SearchResult, *, show_text: bool) -> None:
    print(
        f"\nSearched {len(result.collections_searched)} collection(s): "
        f"{result.candidates} candidates, {len(result.hits)} kept, "
        f"alpha {result.alpha}, re-ranker {result.rerank.value}, {result.took_ms} ms"
    )
    if result.failed_collections:
        failed = ", ".join(c.value for c in result.failed_collections)
        print(f"Not searched (failed or timed out): {failed}")
    if not result.hits:
        print("No matching chunks for this role and these filters.")
        return

    print(f"\n{'#':>2}  {'score':>6}  {'rerank':>6}  {'level':<12}  source")
    for rank, hit in enumerate(result.hits, start=1):
        rerank = f"{hit.rerank_score:.3f}" if hit.rerank_score is not None else "-"
        print(
            f"{rank:>2}  {hit.score:6.3f}  {rerank:>6}  {hit.chunk.access_level.value:<12}  "
            f"{hit.citation.label}"
        )
        if show_text:
            print(textwrap.indent(textwrap.fill(hit.chunk.text, width=96), "      "))


async def _search(settings: Settings, request: SearchRequest, role: Role) -> SearchResult:
    async with open_retriever(settings) as retriever:
        return await retriever.search(request, role=role)


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = _parse_args(argv)
    settings = settings or get_settings()
    if args.no_rerank:
        settings = settings.model_copy(update={"rerank_enabled": False})
    configure_logging("WARNING", LogFormat.CONSOLE)

    try:
        request = SearchRequest(
            query=args.query,
            collections=tuple(args.collections or Collection),
            filters=SearchFilters(
                departments=tuple(args.departments or ()),
                document_types=tuple(args.document_types or ()),
                date_from=args.date_from,
                date_to=args.date_to,
            ),
            top_k=args.top_k,
            alpha=args.alpha,
        )
    except ValidationError as error:
        print(f"Invalid search: {error.errors()[0]['msg']}", file=sys.stderr)
        return EXIT_FAILED

    try:
        result = asyncio.run(_search(settings, request, args.role))
    except RetrievalSetupError as error:
        print(str(error), file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    except RetrievalError as error:
        print(f"{error.message} Check the warnings above.", file=sys.stderr)
        return EXIT_FAILED

    print(f'Search as {args.role.value}: "{request.query}"')
    _print_result(result, show_text=args.show_text)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
