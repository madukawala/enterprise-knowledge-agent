"""The agent service put together from settings, the command line, tracing
and the generated graph diagram. Models and Pinecone are in-memory fakes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langsmith import Client
from pydantic import SecretStr

import agents.service as service
import rag.retrieval as retrieval
from agents import cli, diagram
from agents.llm import LLMGateway, LLMSetupError
from agents.service import AgentService, UnavailableSearcher, open_agent_service
from agents.state import AnswerKind
from agents.tracing import SEARCH_RUN_NAME, TracedSearcher, Tracing
from core.config import Settings
from security.roles import Role
from tests.helpers.agents import agent_service, decision, gateway, search_plan
from tests.helpers.llm import ScriptedChatModel
from tests.helpers.pinecone import FakeAsyncInference, FakeAsyncPinecone
from tests.helpers.search import DIMENSION, index_fixture_documents


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "anthropic_api_key": SecretStr("test-anthropic-key"),
        "claude_model": "anthropic-test",
        "pinecone_api_key": SecretStr("pcsk_test-key"),
        "index_artifacts_dir": tmp_path / "index",
        "embedding_dimension": DIMENSION,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _scripted(*texts: str) -> ScriptedChatModel:
    return ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("certificate expired", ["incidents", "runbooks"])],
        },
        texts=list(texts),
    )


@pytest.fixture
def scripted_model(monkeypatch: pytest.MonkeyPatch) -> ScriptedChatModel:
    """Replace the real Claude/Gemini gateway with a scripted model."""
    model = _scripted("The mTLS certificate had expired [1].")

    def fake_build(settings: Settings, *, metrics: object = None) -> LLMGateway:
        return gateway(model)

    monkeypatch.setattr(service, "build_gateway", fake_build)
    return model


async def test_the_service_answers_from_the_ingested_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted_model: ScriptedChatModel
) -> None:
    index, _ = index_fixture_documents(tmp_path / "index")
    monkeypatch.setattr(
        retrieval,
        "AsyncPinecone",
        FakeAsyncPinecone(fake_index=index, inference=FakeAsyncInference(dimension=DIMENSION)),
    )

    async with open_agent_service(_settings(tmp_path)) as agents:
        run = await agents.ask("Why did card payments fail?", role=Role.ANALYST)

    assert run.answer.kind is AnswerKind.ANSWERED
    assert run.answer.citations[0].citation.doc_id == "INC-TEST-001"
    assert run.search is not None
    assert run.search.hits > 0


async def test_without_the_index_the_agents_still_run_and_say_search_is_unavailable(
    tmp_path: Path, scripted_model: ScriptedChatModel
) -> None:
    # No `make ingest` output in tmp_path, so search cannot be set up.
    async with open_agent_service(_settings(tmp_path)) as agents:
        searcher = agents.graph.nodes  # the graph was built
        run = await agents.ask("Why did card payments fail?", role=Role.ANALYST)

    assert searcher
    assert run.answer.kind is AnswerKind.UNAVAILABLE


async def test_without_a_model_key_setup_fails_with_a_clear_message(tmp_path: Path) -> None:
    with pytest.raises(LLMSetupError, match="ANTHROPIC_API_KEY"):
        async with open_agent_service(_settings(tmp_path, anthropic_api_key=None)):
            pass


async def test_the_unavailable_searcher_always_fails_as_retrieval_error() -> None:
    from core.errors import RetrievalError
    from rag.retrieval import SearchRequest

    with pytest.raises(RetrievalError):
        await UnavailableSearcher("no index").search(SearchRequest(query="x"), role=Role.VIEWER)


# ---------------------------------------------------------------
# Command line
# ---------------------------------------------------------------


def _factory(agents: AgentService):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def factory(settings: Settings) -> AsyncIterator[AgentService]:
        yield agents

    return factory


def test_the_cli_prints_the_answer_and_how_the_run_went(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from tests.helpers.agents import FakeSearcher, hit, result_with

    agents = agent_service(_scripted("Renew it [1]."), FakeSearcher([result_with(hit("RB-001"))]))
    code = cli.main(
        ["Why did it fail?", "--role", "analyst"],
        settings=_settings(tmp_path),
        factory=_factory(agents),
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Renew it [1]." in output
    assert "**How I got this answer**" in output
    assert "nodes: input_guard -> supervisor -> retrieval" in output
    assert "anthropic/anthropic-test (3 calls, 360 tokens)" in output


def test_the_cli_chat_keeps_one_conversation(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agents.checkpoint import memory_checkpointer
    from tests.helpers.agents import FakeSearcher, hit, result_with

    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")] * 2,
            "SearchPlan": [search_plan("one"), search_plan("two")],
        },
        texts=["First [1].", "Second [1]."],
    )
    agents = agent_service(
        model, FakeSearcher([result_with(hit("RB-001"))]), checkpointer=memory_checkpointer()
    )
    lines = iter(["first question", "follow-up", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(lines))

    code = cli.main(["--chat"], settings=_settings(tmp_path), factory=_factory(agents))

    assert code == 0
    assert "First [1]." in capsys.readouterr().out
    assert "User: first question" in model.prompts_for("SearchPlan")[1]


def test_the_cli_reports_a_missing_model_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = cli.main(["hello"], settings=_settings(tmp_path, anthropic_api_key=None))
    assert code == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_the_cli_needs_a_question_or_chat(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([], settings=_settings(tmp_path))
    assert exit_info.value.code == 2


# ---------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------


def test_tracing_is_off_without_a_key(tmp_path: Path) -> None:
    assert not Tracing(_settings(tmp_path)).enabled
    assert not Tracing(_settings(tmp_path, langsmith_tracing=True)).enabled
    assert Tracing(_settings(tmp_path)).callbacks(tags=["x"]) == []


async def test_traced_runs_carry_the_role_and_request_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With tracing on, runs go to our project with our tags, through our client."""
    from langchain_core.tracers.langchain import LangChainTracer

    settings = _settings(
        tmp_path, langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_test-key")
    )
    tracing = Tracing(settings)
    assert tracing.enabled
    (tracer,) = tracing.callbacks(tags=["probe"])
    assert isinstance(tracer, LangChainTracer)
    assert tracer.project_name == "helix-knowledge-agent"

    # Record runs instead of sending them over the network.
    created: list[dict[str, object]] = []
    monkeypatch.setattr(Client, "create_run", lambda self, **run: created.append(run))
    monkeypatch.setattr(Client, "update_run", lambda self, *args, **kwargs: None)

    from tests.helpers.agents import FakeSearcher, hit, result_with

    agents = agent_service(_scripted("Renew it [1]."), FakeSearcher([result_with(hit("RB-1"))]))
    agents.tracing = tracing
    run = await agents.ask("Why?", role=Role.VIEWER, request_id="req-123")

    assert run.request_id == "req-123"
    graph_run = next(r for r in created if r.get("name") == "helix_agent")
    # The trace ID is the run ID we chose, so the UI can link to it.
    assert str(graph_run["id"]) == run.run_id
    assert graph_run["session_name"] == "helix-knowledge-agent"
    metadata = graph_run["extra"]["metadata"]  # type: ignore[index]
    assert metadata["request_id"] == "req-123"
    assert metadata["role"] == "viewer"
    assert {"helix-agent", "role:viewer"} <= set(graph_run["tags"])  # type: ignore[arg-type]
    tracing.flush()


async def test_each_search_is_a_retriever_step_with_the_access_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search shows in the trace under the retrieval agent: the query,
    the exact Pinecone filter (with the role's access levels) and the hits."""
    settings = _settings(
        tmp_path, langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_test-key")
    )
    tracing = Tracing(settings)
    created: list[dict[str, object]] = []
    updated: list[dict[str, object]] = []
    monkeypatch.setattr(Client, "create_run", lambda self, **run: created.append(run))
    monkeypatch.setattr(
        Client, "update_run", lambda self, run_id, **run: updated.append({"id": run_id, **run})
    )

    from tests.helpers.agents import FakeSearcher, hit, result_with

    searcher = TracedSearcher(FakeSearcher([result_with(hit("RB-1"))]))
    agents = agent_service(_scripted("Renew it [1]."), searcher)
    agents.tracing = tracing
    await agents.ask("Why?", role=Role.VIEWER, request_id="req-1")
    tracing.flush()

    retrieval_step = next(r for r in created if r.get("name") == "retrieval")
    search_step = next(r for r in created if r.get("name") == SEARCH_RUN_NAME)
    assert search_step["run_type"] == "retriever"
    assert search_step["parent_run_id"] == retrieval_step["id"]
    inputs = search_step["inputs"]
    assert inputs["query"] == "certificate expired"  # type: ignore[index]
    assert inputs["role"] == "viewer"  # type: ignore[index]
    assert inputs["filter"] == {"access_level": {"$in": ["internal", "public"]}}  # type: ignore[index]

    (end,) = [u for u in updated if u["id"] == search_step["id"]]
    (document,) = end["outputs"]["documents"]  # type: ignore[index]
    assert document["metadata"]["doc_id"] == "RB-1"
    assert document["metadata"]["access_level"] == "internal"


async def test_searches_outside_a_trace_send_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No traced run around the search: no step is sent through a default client."""
    created: list[dict[str, object]] = []
    monkeypatch.setattr(Client, "create_run", lambda self, **run: created.append(run))
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    from rag.retrieval import SearchRequest
    from tests.helpers.agents import FakeSearcher, hit, result_with

    result = await TracedSearcher(FakeSearcher([result_with(hit("RB-1"))])).search(
        SearchRequest(query="q"), role=Role.VIEWER
    )

    assert [h.chunk.doc_id for h in result.hits] == ["RB-1"]
    assert created == []


def _feedback_recorder(
    monkeypatch: pytest.MonkeyPatch, *, project_found: bool = True
) -> tuple[list[dict[str, object]], list[str]]:
    """Record feedback and project lookups instead of calling LangSmith."""
    sent: list[dict[str, object]] = []
    lookups: list[str] = []

    def read_project(self: Client, *, project_name: str) -> object:
        lookups.append(project_name)
        if not project_found:
            raise RuntimeError("not found")
        return SimpleNamespace(id="project-1")

    monkeypatch.setattr(Client, "read_project", read_project)
    monkeypatch.setattr(Client, "create_feedback", lambda self, **feedback: sent.append(feedback))
    return sent, lookups


def test_feedback_names_the_project_of_the_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LangSmith wants the project with each feedback; it is looked up once."""
    sent, lookups = _feedback_recorder(monkeypatch)
    settings = _settings(
        tmp_path, langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_test-key")
    )
    tracing = Tracing(settings)

    assert tracing.send_feedback("run-1", score=1.0, comment=None)
    assert tracing.send_feedback("run-2", score=0.0, comment=None)

    assert [f["session_id"] for f in sent] == ["project-1", "project-1"]
    assert lookups == ["helix-knowledge-agent"]


def test_feedback_is_still_sent_when_the_project_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, _ = _feedback_recorder(monkeypatch, project_found=False)
    settings = _settings(
        tmp_path, langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_test-key")
    )

    assert Tracing(settings).send_feedback("run-1", score=1.0, comment=None)
    assert sent[0]["session_id"] is None


def test_run_config_labels_every_trace() -> None:
    config = service.run_config(role=Role.ANALYST, thread_id="t-1", request_id="r-1")
    assert config["configurable"] == {"thread_id": "t-1"}
    assert config["metadata"] == {"request_id": "r-1", "thread_id": "t-1", "role": "analyst"}
    assert config["tags"] == ["role:analyst"]


# ---------------------------------------------------------------
# The generated diagram
# ---------------------------------------------------------------


def test_the_generated_graph_diagram_matches_the_code() -> None:
    """Run `make diagrams` if this fails: the graph changed but the diagram did not."""
    committed = Path("docs/architecture/03-agent-graph.generated.md").read_text(encoding="utf-8")
    assert committed == diagram.render()


def test_the_diagram_command_writes_the_file(tmp_path: Path) -> None:
    target = tmp_path / "graph.md"
    assert diagram.main(["--output", str(target)]) == 0
    assert "retrieval --> supervisor" in target.read_text(encoding="utf-8")
