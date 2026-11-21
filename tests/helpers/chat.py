"""An API app with scripted agents, for tests of `POST /chat`.

model = answering_model("Raise the pool size [1].")
app = chat_app(settings, model, FakeSearcher([result_with(hit("RB-001"))]))
reply = await post_stream(app, "/chat", {"message": "pool?"})
"""

from typing import Any

from fastapi import FastAPI

from agents.checkpoint import memory_checkpointer
from agents.llm import LLMGateway
from api.main import create_app
from core.config import Settings
from tests.helpers.agents import FakeSearcher, agent_service, decision, search_plan
from tests.helpers.llm import ScriptedChatModel
from tests.helpers.tokens import TOKENS


def answering_model(*texts: Any, **options: Any) -> ScriptedChatModel:
    """A model that plans a document search and then writes `texts`, one per answer."""
    questions = max(len(texts), 1)
    return ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")] * questions,
            "SearchPlan": [search_plan("pool exhausted", ["runbooks"])] * questions,
        },
        texts=list(texts),
        **options,
    )


def chat_app(
    settings: Settings,
    model: ScriptedChatModel | LLMGateway,
    searcher: FakeSearcher | None = None,
) -> FastAPI:
    """The real app, with the agents it would open on startup replaced by scripted ones."""
    app = create_app(settings, signing_keys=TOKENS.signing_keys)
    app.state.agents = agent_service(
        model, searcher or FakeSearcher(), checkpointer=memory_checkpointer()
    )
    return app
