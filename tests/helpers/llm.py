"""Fake chat models, so tests never call Claude or Gemini.

- `fake_chat_model(["reply", ...])` answers plain chat calls in order.
- `ScriptedChatModel` also answers structured calls (`with_structured_output`),
  which the agents use for the supervisor and the search plan. Replies are
  scripted per schema name, so a test reads like a conversation:

      model = ScriptedChatModel(
          structured={"SupervisorDecision": [{"intent": "document_question", ...}]},
          texts=["The pool limit is 200 [1]."],
      )

  A scripted `Exception` is raised instead of answering, and `delay` makes
  every call slow, for timeout tests. `calls` records what was sent.

  It also streams, like the real models do when LangGraph asks for tokens:
  a text reply arrives word by word (`token_delay` apart). A reply such as
  `PartialFailure("Half an ans", RuntimeError())` streams some words and
  then fails, to test a model breaking in the middle of an answer.

This keeps tests free, fast and the same on every run.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class PartialFailure:
    """Stream `text`, then raise `error` (only when streamed)."""

    text: str
    error: Exception


Reply = str | dict[str, Any] | BaseModel | Exception | PartialFailure

USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


def fake_chat_model(replies: Iterable[str | AIMessage]) -> GenericFakeChatModel:
    """Return a chat model that answers with `replies`, one per call."""
    messages = [reply if isinstance(reply, AIMessage) else AIMessage(reply) for reply in replies]
    return GenericFakeChatModel(messages=iter(messages))


class ScriptedChatModel(BaseChatModel):
    structured: dict[str, list[Reply]] = Field(default_factory=dict)
    texts: list[Reply] = Field(default_factory=list)
    delay: float = 0.0
    token_delay: float = 0.0
    calls: list[tuple[str, list[BaseMessage]]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tools = kwargs.get("tools")
        if tools:
            name = tools[0]["function"]["name"]
            reply = self._next(self.structured.get(name, []), name, messages)
            args = reply.model_dump(mode="json") if isinstance(reply, BaseModel) else reply
            message = AIMessage(
                "",
                tool_calls=[{"name": name, "args": args, "id": "call-1", "type": "tool_call"}],
                usage_metadata=USAGE,  # type: ignore[arg-type]
            )
        else:
            reply = self._next(self.texts, "text", messages)
            if isinstance(reply, PartialFailure):
                raise reply.error
            message = AIMessage(str(reply), usage_metadata=USAGE)  # type: ignore[arg-type]
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if self.delay:
            await asyncio.sleep(self.delay)
        if kwargs.get("tools"):
            # Structured output arrives as one tool-call chunk.
            message = self._generate(messages, stop, **kwargs).generations[0].message
            assert isinstance(message, AIMessage)
            call = message.tool_calls[0]
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    "",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call["args"]),
                            "id": call["id"],
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                    usage_metadata=USAGE,  # type: ignore[arg-type]
                )
            )
            return
        reply = self._next(self.texts, "text", messages)
        text = reply.text if isinstance(reply, PartialFailure) else str(reply)
        for word in re.findall(r"\S+\s*|\s+", text):
            if self.token_delay:
                await asyncio.sleep(self.token_delay)
            chunk = ChatGenerationChunk(message=AIMessageChunk(word))
            if run_manager:
                await run_manager.on_llm_new_token(word, chunk=chunk)
            yield chunk
        if isinstance(reply, PartialFailure):
            raise reply.error
        yield ChatGenerationChunk(message=AIMessageChunk("", usage_metadata=USAGE))  # type: ignore[arg-type]

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._generate(messages, stop, **kwargs)

    def _next(self, queue: list[Reply], name: str, messages: list[BaseMessage]) -> Any:
        self.calls.append((name, list(messages)))
        if not queue:
            raise AssertionError(f"ScriptedChatModel has no reply left for {name!r}")
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def prompts_for(self, name: str) -> list[str]:
        """The full text of every prompt sent for `name` ("text" for plain calls)."""
        return [
            "\n".join(message.text for message in messages)
            for call_name, messages in self.calls
            if call_name == name
        ]
