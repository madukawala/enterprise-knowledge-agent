"""Ask the agents a question from the terminal.

    make ask Q="Why do card payments fail?" ROLE=analyst
    make chat ROLE=viewer          # several questions in one conversation
    uv run python -m agents.cli --help

Needs GEMINI_API_KEY or OPENROUTER_API_KEY, and the Pinecone index from
`make ingest`. Without the index, the agents still run and answer that
document search is unavailable.

`--role` is for local testing, like `make search`. In the app the role
comes from the login token.

Exit codes: 0 answered, 1 setup problem (for example no model key),
2 bad arguments.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from agents.llm import LLMSetupError
from agents.service import AgentRun, AgentService, open_agent_service
from core.config import Settings, get_settings
from core.logging import configure_logging
from security.roles import Role

ServiceFactory = Callable[[Settings], AbstractAsyncContextManager[AgentService]]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agents.cli",
        description="Ask the Helix Bank knowledge assistant a question.",
    )
    parser.add_argument("question", nargs="?", help="the question (leave out with --chat)")
    parser.add_argument(
        "--role", type=Role, choices=list(Role), default=Role.VIEWER, help="default: viewer"
    )
    parser.add_argument(
        "--chat", action="store_true", help="keep asking questions in one conversation"
    )
    return parser


def render(run: AgentRun) -> str:
    """The answer, then one line on how the run went."""
    models = sorted({f"{call.provider}/{call.model}" for call in run.llm_calls})
    tokens = sum(call.input_tokens + call.output_tokens for call in run.llm_calls)
    footer = [
        "nodes: " + " -> ".join(run.nodes),
        f"models: {', '.join(models) or 'none'} ({len(run.llm_calls)} calls, {tokens} tokens)",
        f"{run.duration_ms / 1000:.1f} s",
        f"request {run.request_id}",
    ]
    return f"{run.answer.to_markdown()}\n\n---\n" + " | ".join(footer)


async def _run(args: argparse.Namespace, settings: Settings, factory: ServiceFactory) -> int:
    try:
        async with factory(settings) as agents:
            if not args.chat:
                print(render(await agents.ask(args.question, role=args.role)))
                return 0
            thread_id = None
            print(f"Asking as {args.role.value}. An empty line ends the conversation.")
            while question := (await asyncio.to_thread(input, "\nYou: ")).strip():
                run = await agents.ask(question, role=args.role, thread_id=thread_id)
                thread_id = run.thread_id
                print("\n" + render(run))
            return 0
    except LLMSetupError as error:
        print(error, file=sys.stderr)
        return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    factory: ServiceFactory = open_agent_service,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.chat and not args.question:
        parser.error("give a question, or use --chat")
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        return asyncio.run(_run(args, settings, factory))
    except (KeyboardInterrupt, EOFError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
