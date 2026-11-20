"""Fake chat models, so tests never call Gemini or Groq.

The fake model returns the replies you give it, in order. This keeps tests
free, fast and the same on every run.
"""

from collections.abc import Iterable

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


def fake_chat_model(replies: Iterable[str | AIMessage]) -> GenericFakeChatModel:
    """Return a chat model that answers with `replies`, one per call."""
    messages = [reply if isinstance(reply, AIMessage) else AIMessage(reply) for reply in replies]
    return GenericFakeChatModel(messages=iter(messages))
