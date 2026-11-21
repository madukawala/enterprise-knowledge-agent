"""The MCP server: three read-only tools over the enterprise data.

    find_person(name)                         the employee directory
    service_owner(service)                    the service catalog: owner team, owner, on-call
    list_incidents(service, status, limit)    current incident records

Every argument is typed and bounded, and the MCP SDK checks it against the
tool's schema before the tool runs: a wrong type, a missing field or an
unknown status is refused with an error, never guessed. Text arguments are
matched against the data, never used to build a query. Results are small
JSON objects, at most `MAX_RESULTS` items.

The data is fictional (data/enterprise.json) and read once at start-up.
The tools only read it; nothing a caller sends can change it.

Run:  python -m mcp_server     (streamable HTTP on port 8200, path /mcp)
"""

import json
import os
from functools import cache
from importlib.resources import files
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

MAX_RESULTS = 20
Status = Literal["open", "monitoring", "resolved"]


@cache
def data() -> dict[str, list[dict[str, Any]]]:
    raw = json.loads(files(__package__).joinpath("data/enterprise.json").read_text("utf-8"))
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def _person(person_id: str) -> dict[str, Any] | None:
    return next((p for p in data()["people"] if p["id"] == person_id), None)


def _brief(person_id: str) -> dict[str, Any] | None:
    person = _person(person_id)
    if person is None:
        return None
    return {key: person[key] for key in ("name", "title", "team", "email")}


def _matches(text: str, wanted: str) -> bool:
    return wanted.casefold() in text.casefold()


def find_person(
    name: Annotated[str, Field(min_length=2, max_length=60, description="A name or part of one.")],
) -> dict[str, Any]:
    """Look up people in the employee directory by name: title, team, location and e-mail."""
    found = [p for p in data()["people"] if _matches(p["name"], name.strip())]
    return {"people": found[:MAX_RESULTS], "count": len(found)}


def service_owner(
    service: Annotated[
        str, Field(min_length=2, max_length=60, description="A service name, e.g. PayHub.")
    ],
) -> dict[str, Any]:
    """Who owns a service: the owner team, the service owner and who is on call."""
    found = [s for s in data()["services"] if _matches(s["name"], service.strip())]
    return {
        "services": [
            {
                **{k: v for k, v in s.items() if k not in {"service_owner", "on_call"}},
                "service_owner": _brief(s["service_owner"]),
                "on_call": _brief(s["on_call"]),
            }
            for s in found[:MAX_RESULTS]
        ],
        "count": len(found),
    }


def list_incidents(
    service: Annotated[
        str | None, Field(default=None, max_length=60, description="Only this service.")
    ] = None,
    status: Annotated[Status | None, Field(default=None, description="Only this status.")] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_RESULTS, description="How many, newest first.")] = 10,
) -> dict[str, Any]:
    """The current incident records, newest first: status, severity, assignee and summary."""
    found = [
        i
        for i in data()["incidents"]
        if (service is None or _matches(i["service"], service.strip()))
        and (status is None or i["status"] == status)
    ]
    found.sort(key=lambda i: i["opened"], reverse=True)
    return {
        "incidents": [
            {**{k: v for k, v in i.items() if k != "assignee"}, "assignee": _brief(i["assignee"])}
            for i in found[:limit]
        ],
        "count": len(found),
    }


def build_server() -> MCPServer:
    server = MCPServer(
        "helix-enterprise-data",
        instructions="Read-only enterprise data for Helix Commercial Bank (fictional).",
    )
    for tool in (find_person, service_owner, list_incidents):
        server.add_tool(tool)

    @server.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return server


def main() -> None:
    port = int(os.environ.get("MCP_PORT", "8200"))
    # Only requests addressed to this service by its Compose name (or from
    # inside the container) are served: the DNS-rebinding check stays on.
    hosts = os.environ.get("MCP_ALLOWED_HOSTS", f"mcp:{port},localhost:{port},127.0.0.1:{port}")
    build_server().run(
        transport="streamable-http",
        host="0.0.0.0",  # noqa: S104  # nosec B104 - inside its own container
        port=port,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        ),
    )


if __name__ == "__main__":
    main()
