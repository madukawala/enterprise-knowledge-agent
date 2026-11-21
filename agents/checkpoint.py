"""Checkpointing: where LangGraph saves the state after every step.

The checkpointer is what keeps a conversation going: the next question in
the same thread starts from the saved state, so follow-ups work.

- For now the state is kept in memory (`InMemorySaver`), which lasts as long
  as the process. `feature/memory` swaps in the Postgres checkpointer with
  no change to the graph.
- Saved state is read back with an explicit list of allowed types. By
  default LangGraph rebuilds any Python class named in a checkpoint and only
  logs a warning; with this list, a tampered checkpoint cannot make the app
  build an unexpected object.
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agents.state import STATE_TYPES
from rag.filters import SearchFilters
from rag.retrieval import Citation, RerankStatus, SearchHit
from rag.schema import ChunkMetadata, Collection, Department, DocumentType
from security.roles import AccessLevel

# Search types stored inside the state (hits, filters, citations).
_SEARCH_TYPES: tuple[type, ...] = (
    SearchHit,
    ChunkMetadata,
    Citation,
    SearchFilters,
    RerankStatus,
    Collection,
    Department,
    DocumentType,
    AccessLevel,
)

ALLOWED_TYPES: tuple[type, ...] = (*STATE_TYPES, *_SEARCH_TYPES)


def serializer() -> JsonPlusSerializer:
    """A serializer that rebuilds only LangChain messages and the types above."""
    return JsonPlusSerializer(
        allowed_msgpack_modules=[(kind.__module__, kind.__name__) for kind in ALLOWED_TYPES]
    )


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=serializer())
