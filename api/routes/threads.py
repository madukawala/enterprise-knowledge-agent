"""The signed-in user's conversations and memory.

    GET    /threads              the caller's conversations, most recent first
    GET    /threads/{thread_id}  one conversation's questions and answers
    DELETE /threads/{thread_id}  delete a conversation and everything saved for it
    GET    /me/memory            what the assistant remembers about the caller
    DELETE /me/memory            forget all of it

Every route works on the caller's own data only: threads are checked
against their owner (anyone else gets 404, as for `POST /chat`), and memory
is keyed by the caller's pseudonym from the login token. There is no way to
name another user.
"""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from agents.service import AgentService
from api.identity import Caller, requires
from api.threads import ThreadRegistry
from core.errors import LLMUnavailableError
from core.logging import get_logger
from security.policies import Action

router = APIRouter(tags=["conversations"])
log = get_logger(__name__)

NOT_READY = "Saved conversations are not available right now. Please try again later."

SignedIn = Annotated[Caller, Depends(requires(Action.CHAT))]


class ThreadSummary(BaseModel):
    thread_id: str
    title: str
    created: datetime
    updated: datetime


class Message(BaseModel):
    role: str  # user or assistant
    text: str


class ThreadDetail(ThreadSummary):
    messages: list[Message]


class MemoryItem(BaseModel):
    text: str
    documents: list[str]
    at: datetime


class MemoryResponse(BaseModel):
    # How often each department came up in the caller's searches.
    departments: dict[str, int]
    interactions: list[MemoryItem]


class Forgotten(BaseModel):
    deleted: int


def _threads(request: Request) -> ThreadRegistry:
    return cast(ThreadRegistry, request.app.state.threads)


def _agents(request: Request) -> AgentService:
    agents = cast(AgentService | None, getattr(request.app.state, "agents", None))
    if agents is None:
        raise LLMUnavailableError(NOT_READY)
    return agents


@router.get("/threads")
async def list_threads(request: Request, caller: SignedIn) -> list[ThreadSummary]:
    threads = await _threads(request).of_owner(caller.owner)
    return [ThreadSummary(**vars(thread)) for thread in threads]


@router.get("/threads/{thread_id}", responses={404: {"description": "Not the caller's."}})
async def get_thread(thread_id: str, request: Request, caller: SignedIn) -> ThreadDetail:
    thread = await _threads(request).get(thread_id, owner=caller.owner)
    messages = await _agents(request).history(thread_id)
    return ThreadDetail(**vars(thread), messages=[Message(**m) for m in messages])


@router.delete(
    "/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Not the caller's."}, 409: {"description": "Answering."}},
)
async def delete_thread(thread_id: str, request: Request, caller: SignedIn) -> Response:
    await _threads(request).delete(thread_id, owner=caller.owner)
    await _agents(request).forget_thread(thread_id)
    log.info("thread_deleted", thread_id=thread_id, user=caller.pseudonym)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/memory")
async def my_memory(request: Request, caller: SignedIn) -> MemoryResponse:
    memory = _agents(request).memory
    if memory is None:
        return MemoryResponse(departments={}, interactions=[])
    profile = await memory.profile(caller.pseudonym)
    items = await memory.interactions(caller.pseudonym)
    return MemoryResponse(
        departments={str(k): int(v) for k, v in (profile.get("departments") or {}).items()},
        interactions=[
            MemoryItem(text=item.text, documents=list(item.documents), at=item.at) for item in items
        ],
    )


@router.delete("/me/memory")
async def forget_me(request: Request, caller: SignedIn) -> Forgotten:
    memory = _agents(request).memory
    deleted = await memory.forget(caller.pseudonym) if memory is not None else 0
    log.info("memory_forgotten", user=caller.pseudonym, items=deleted)
    return Forgotten(deleted=deleted)
