"""Role x endpoint x tool: who gets what, table-driven.

Every API route is listed here with the status each caller should get. A
new route that is not in `ROUTES` fails `test_every_route_is_in_the_matrix`,
so nobody can add an endpoint without deciding who may call it.
"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from core.config import Settings
from rag.filters import build_filter
from security.roles import AccessLevel, Role
from tests.helpers.agents import FakeSearcher, agent_service, decision, hit, result_with
from tests.helpers.agents import search_plan as plan_of
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.llm import ScriptedChatModel
from tests.helpers.sandbox import LocalSandbox
from tests.helpers.sse import post_stream
from tests.helpers.tokens import TOKENS, TestUser, bearer, bearer_for

ANON = None
V, A, AD = Role.VIEWER, Role.ANALYST, Role.ADMIN

# (method, path) -> {caller: expected status}. ANON is "no token".
ROUTES: dict[tuple[str, str], dict[Role | None, int]] = {
    ("GET", "/healthz"): {ANON: 200, V: 200, A: 200, AD: 200},
    ("GET", "/readyz"): {ANON: 200, V: 200, A: 200, AD: 200},
    ("GET", "/me"): {ANON: 401, V: 200, A: 200, AD: 200},
    ("POST", "/chat"): {ANON: 401, V: 200, A: 200, AD: 200},
    ("POST", "/tools/python"): {ANON: 401, V: 403, A: 200, AD: 200},
    ("GET", "/threads"): {ANON: 401, V: 200, A: 200, AD: 200},
    # The thread belongs to nobody here, so every signed-in caller gets 404.
    ("GET", "/threads/{thread_id}"): {ANON: 401, V: 404, A: 404, AD: 404},
    ("DELETE", "/threads/{thread_id}"): {ANON: 401, V: 404, A: 404, AD: 404},
    ("GET", "/me/memory"): {ANON: 401, V: 200, A: 200, AD: 200},
    ("DELETE", "/me/memory"): {ANON: 401, V: 200, A: 200, AD: 200},
    ("POST", "/admin/memory/cleanup"): {ANON: 401, V: 403, A: 403, AD: 200},
    # Nothing waits on this thread, so every signed-in caller gets 409.
    ("POST", "/approvals/{thread_id}"): {ANON: 401, V: 409, A: 409, AD: 409},
    ("POST", "/feedback"): {ANON: 401, V: 201, A: 201, AD: 201},
    ("GET", "/admin/feedback"): {ANON: 401, V: 403, A: 403, AD: 200},
    ("GET", "/admin/audit-log"): {ANON: 401, V: 403, A: 403, AD: 200},
    ("GET", "/admin/access-matrix"): {ANON: 401, V: 403, A: 403, AD: 200},
    ("GET", "/admin/rate-limits"): {ANON: 401, V: 403, A: 403, AD: 200},
    ("PUT", "/admin/rate-limits/{role}"): {ANON: 401, V: 403, A: 403, AD: 200},
    ("DELETE", "/admin/rate-limits/{role}"): {ANON: 401, V: 403, A: 403, AD: 200},
}
# A valid body and path values for routes that need them, so an allowed
# caller gets 200 rather than 422.
BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("POST", "/tools/python"): {"code": "result = 1 + 1"},
    ("PUT", "/admin/rate-limits/{role}"): {"per_minute": 10, "daily_tokens": 100_000},
    ("POST", "/approvals/{thread_id}"): {"approve": True},
    ("POST", "/feedback"): {
        "run_id": "6d4181d2-0792-4666-9529-0b52284f3c73",
        "thread_id": "someone-elses-thread",
        "score": "up",
    },
}
PATH_VALUES = {"{role}": "viewer", "{thread_id}": "someone-elses-thread"}
# Not listed: /metrics, /docs and /openapi.json, which are open on purpose
# (the docs are switched off in production, see api/main.py).

CASES = [
    (method, path, caller, status)
    for (method, path), expected in ROUTES.items()
    for caller, status in expected.items()
]


def _id(case: tuple[str, str, Role | None, int]) -> str:
    method, path, caller, status = case
    return f"{method} {path} as {caller.value if caller else 'anonymous'} -> {status}"


@pytest.fixture
def matrix_app(settings: Settings) -> FastAPI:
    searcher = FakeSearcher([result_with(hit("RB-001"))])
    app = chat_app(settings, answering_model(*["Answer [1]."] * 20), searcher)
    app.state.sandbox = LocalSandbox()
    return app


async def _call(app: FastAPI, method: str, path: str, caller: Role | None) -> int:
    headers = bearer(caller) if caller is not None else {}
    if method == "POST" and path == "/chat":
        reply = await post_stream(app, path, {"message": "pool?"}, role=None, headers=headers)
        return reply.status
    body = BODIES.get((method, path))
    for name, value in PATH_VALUES.items():
        path = path.replace(name, value)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return (await http.request(method, path, headers=headers, json=body)).status_code


@pytest.mark.parametrize(("method", "path", "caller", "status"), CASES, ids=map(_id, CASES))
async def test_each_role_gets_the_expected_status_on_each_route(
    matrix_app: FastAPI, method: str, path: str, caller: Role | None, status: int
) -> None:
    assert await _call(matrix_app, method, path, caller) == status


def test_every_route_is_in_the_matrix(matrix_app: FastAPI) -> None:
    # The OpenAPI schema lists every route the routers define, with its
    # methods. /metrics, /docs and /openapi.json are not in it.
    app_routes = {
        (method.upper(), path)
        for path, operations in matrix_app.openapi()["paths"].items()
        for method in operations
    }
    assert app_routes == set(ROUTES)


async def test_me_shows_each_role_its_own_permissions(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        bodies = {role: (await http.get("/me", headers=bearer(role))).json() for role in Role}

    def allowed(role: Role) -> set[str]:
        return {p["action"] for p in bodies[role]["permissions"] if p["allowed"]}

    assert allowed(V) == {"chat", "knowledge_search"}
    assert allowed(A) == allowed(V) | {"python_analysis", "mcp_tools", "recursive_research"}
    assert {a for a in allowed(AD) if a.startswith("admin_")} == {
        "admin_access_matrix",
        "admin_audit_log",
        "admin_rate_limits",
        "admin_all_feedback",
        "admin_reindex",
        "admin_memory_cleanup",
    }
    assert bodies[V]["access_levels"] == ["public", "internal"]
    assert bodies[AD]["access_levels"] == [level.value for level in AccessLevel]
    assert bodies[A]["role"] == "analyst"
    assert bodies[A]["username"] == "adam.analyst"


async def test_the_admin_matrix_endpoint_returns_the_whole_table(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        body = (await http.get("/admin/access-matrix", headers=bearer(AD))).json()

    assert body["actions"]["python_analysis"] == {"viewer": False, "analyst": True, "admin": True}
    assert body["access_levels"]["viewer"] == ["public", "internal"]


# ---------------------------------------------------------------
# Role x data: what each role's search may return
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "levels"),
    [
        (V, {"public", "internal"}),
        (A, {"public", "internal", "confidential"}),
        (AD, {"public", "internal", "confidential", "restricted"}),
    ],
)
async def test_the_search_runs_as_the_role_in_the_token(
    settings: Settings, role: Role, levels: set[str]
) -> None:
    searcher = FakeSearcher([result_with(hit("RB-001"))])
    app = chat_app(settings, answering_model("Answer [1]."), searcher)

    await post_stream(app, "/chat", {"message": "pool?"}, role=role)

    ((_, searched_as),) = searcher.requests
    assert searched_as is role
    assert set(build_filter(searched_as)["access_level"]["$in"]) == levels


# ---------------------------------------------------------------
# Role x tool: what the agents offer each role
# ---------------------------------------------------------------


def _research_model() -> ScriptedChatModel:
    return ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("research", ["Find outages", "Group causes"])],
            "SearchPlan": [plan_of("payment outages", ["incidents"])],
            "BatchFindings": [{"findings": []}] * 5,
        },
        texts=["Two root causes [1]."],
    )


@pytest.mark.parametrize(
    ("role", "gets_research"),
    [(V, False), (A, True), (AD, True)],
)
async def test_research_is_only_offered_to_roles_that_may_use_it(
    role: Role, gets_research: bool
) -> None:
    searcher = FakeSearcher([result_with(hit("INC-1"))])

    run = await agent_service(_research_model(), searcher).ask("outages last year?", role=role)

    limits = " ".join(run.answer.limits)
    assert ("research" in run.nodes) is gets_research
    assert {searched_as for _, searched_as in searcher.requests} == {role}
    if gets_research:
        assert "cannot use" not in limits
    else:
        assert run.nodes.count("retrieval") == 1
        assert f"Your role ({role.value}) cannot use deep research" in limits


@pytest.mark.parametrize(("role", "allowed"), [(V, False), (A, True), (AD, True)])
async def test_enterprise_data_tools_are_only_offered_to_roles_that_may_use_them(
    role: Role, allowed: bool
) -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("enterprise_data")],
            "SearchPlan": [plan_of("owner of PayHub")],
        },
        texts=["The Payments Platform team [1]."],
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("ARC-001"))])).ask(
        "Who owns PayHub?", role=role
    )
    denied = any("cannot use enterprise data tools" in limit for limit in run.answer.limits)
    assert denied is not allowed


# ---------------------------------------------------------------
# Conversations belong to a user, not to a role
# ---------------------------------------------------------------


async def test_two_users_with_the_same_role_do_not_share_a_conversation(
    settings: Settings,
) -> None:
    model = answering_model("Private answer [1].", "Second [1].")
    app = chat_app(settings, model, FakeSearcher([result_with(hit("RB-001"))]))
    first_analyst = TestUser(username="first.analyst", role=A)
    second_analyst = TestUser(username="second.analyst", role=A)

    first = await post_stream(
        app, "/chat", {"message": "my question"}, role=None, headers=bearer_for(first_analyst)
    )
    thread_id = first.headers["x-thread-id"]
    second = await post_stream(
        app,
        "/chat",
        {"message": "what did they ask?", "thread_id": thread_id},
        role=None,
        headers=bearer_for(second_analyst),
    )

    assert second.status == 404
    assert len(model.prompts_for("text")) == 1


async def test_the_same_user_can_continue_their_conversation(settings: Settings) -> None:
    model = answering_model("First [1].", "Second [1].")
    app = chat_app(settings, model, FakeSearcher([result_with(hit("RB-001"))]))
    user = TestUser(username="returning.user", role=V)

    first = await post_stream(app, "/chat", {"message": "q1"}, role=None, headers=bearer_for(user))
    body: dict[str, Any] = {"message": "q2", "thread_id": first.headers["x-thread-id"]}
    # A fresh token for the same user, as after a refresh.
    second = await post_stream(app, "/chat", body, role=None, headers=bearer_for(user, TOKENS))

    assert second.status == 200
    assert second.types[-1] == "response_end"
