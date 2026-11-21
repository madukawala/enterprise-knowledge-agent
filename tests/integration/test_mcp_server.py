"""The MCP server's tools, called through the real MCP client in memory."""

import json
from typing import Any

import pytest
from mcp.client.client import Client

from agents.tools.enterprise import EnterpriseTools, check_arguments
from mcp_server.server import MAX_RESULTS, build_server, data


async def _call(tool: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    async with Client(build_server()) as client:
        reply = await client.call_tool(tool, arguments)
    text = "".join(getattr(part, "text", "") for part in reply.content)
    return reply.is_error, (text if reply.is_error else json.loads(text))


async def test_the_server_offers_exactly_three_read_only_tools() -> None:
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"find_person", "service_owner", "list_incidents"}
    schema = tools["list_incidents"].input_schema
    assert schema["properties"]["limit"]["maximum"] == MAX_RESULTS
    assert set(schema["properties"]["status"]["anyOf"][0]["enum"]) == {
        "open",
        "monitoring",
        "resolved",
    }


async def test_service_owner_names_the_team_owner_and_on_call() -> None:
    error, body = await _call("service_owner", {"service": "payhub"})
    assert not error
    (service,) = body["services"]
    assert service["owner_team"] == "Payments Platform"
    assert service["service_owner"]["name"] == "Grace Okafor"
    assert service["on_call"]["name"] == "Tomas Varga"


async def test_find_person_matches_part_of_a_name() -> None:
    _, body = await _call("find_person", {"name": "okafor"})
    assert [p["name"] for p in body["people"]] == ["Grace Okafor"]


async def test_incidents_are_filtered_and_newest_first() -> None:
    _, body = await _call("list_incidents", {"status": "open"})
    opened = [i["opened"] for i in body["incidents"]]
    assert opened == sorted(opened, reverse=True)
    assert all(i["status"] == "open" for i in body["incidents"])
    _, payhub = await _call("list_incidents", {"service": "PayHub", "limit": 1})
    assert len(payhub["incidents"]) == 1


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_incidents", {"status": "deleted"}),
        ("list_incidents", {"limit": 500}),
        ("list_incidents", {"limit": "all"}),
        ("find_person", {"name": "x"}),
        ("find_person", {}),
        ("service_owner", {"service": "a" * 200}),
    ],
)
async def test_bad_arguments_are_refused_by_the_server(
    tool: str, arguments: dict[str, Any]
) -> None:
    error, _ = await _call(tool, arguments)
    assert error


async def test_the_server_reports_an_unknown_tool_as_an_error() -> None:
    error, _ = await _call("delete_everything", {})
    assert error


def test_the_data_is_fictional_and_consistent() -> None:
    people = {p["id"] for p in data()["people"]}
    assert all(p["email"].endswith("@helix.example") for p in data()["people"])
    for service in data()["services"]:
        assert {service["service_owner"], service["on_call"]} <= people
    assert {i["assignee"] for i in data()["incidents"]} <= people


# ---------------------------------------------------------------
# The agent-side client
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "reason"),
    [
        ("drop_tables", {}, "unknown tool"),
        ("list_incidents", "status=open", "must be an object"),
        ("list_incidents", {"status": "closed"}, "bad arguments (status)"),
        ("service_owner", {"service": "PayHub", "admin": True}, "bad arguments (admin)"),
    ],
)
def test_arguments_are_checked_before_anything_is_sent(
    tool: str, arguments: Any, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        check_arguments(tool, arguments)


async def test_the_client_returns_data_and_counts() -> None:
    result = await EnterpriseTools(build_server()).call("list_incidents", {"status": "open"})
    assert result.ok
    assert result.items == len(result.data["incidents"])
    assert result.as_text().startswith('{"incidents":')


async def test_a_refused_call_never_reaches_the_server() -> None:
    class Unreachable:
        pass

    result = await EnterpriseTools(Unreachable()).call("list_incidents", {"limit": 99})
    assert not result.ok
    assert result.error == "bad arguments (limit)"


async def test_a_server_that_is_down_gives_a_safe_error() -> None:
    tools = EnterpriseTools("http://127.0.0.1:9/mcp", timeout_seconds=2)
    result = await tools.call("service_owner", {"service": "PayHub"})
    assert not result.ok
    assert result.error == "the enterprise data service did not respond"
