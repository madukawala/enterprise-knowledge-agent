# mcp_server

An MCP (Model Context Protocol) server with Helix Bank's enterprise data: the employee directory, the service catalog and the incident records. The assistant's tools agent uses it to answer questions such as "who owns PayHub?" or "which incidents are open right now?" for analysts and admins.

All of the data is fictional, in [data/enterprise.json](data/enterprise.json): 12 people, 8 services and 8 incident records, using the same systems and teams as the sample documents.

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `find_person` | `name` (2 to 60 characters; part of a name is enough) | Matching people: title, team, department, location, work e-mail |
| `service_owner` | `service` (2 to 60 characters) | The owner team, the service owner and who is on call |
| `list_incidents` | `service` (optional), `status` (`open`, `monitoring` or `resolved`, optional), `limit` (1 to 20, default 10) | Incident records, newest first: severity, status, dates, assignee, summary |

All three only read. Arguments are typed and bounded, and the MCP SDK checks them against each tool's schema before the tool runs: a wrong type, an unknown status or a limit of 500 is refused with an error. Text arguments are only compared with the data, never used to build a query.

## Who can use it

| Layer | Control |
|---|---|
| Role | Only roles with the `mcp_tools` permission (analysts, admins) reach the tools agent: the supervisor checks the role matrix, and the tools agent checks it again before calling anything |
| Tool choice | The model picks from these three tools only; the call and its arguments are checked in code against the same limits before anything is sent ([agents/tools/enterprise.py](../agents/tools/enterprise.py)) |
| Network | The server is only on the internal `tools` Docker network, shared with the API alone: no published port, no internet, and nothing else (the UI, the sandbox) can reach it |
| Host check | The SDK's DNS-rebinding protection stays on; only requests addressed to `mcp:8200` (or from inside the container) are served |
| Container | Non-root user, read-only file system, every Linux capability dropped, 256 MB memory |

Results reach the answer prompt inside `<tool_results>` tags, which the system prompt treats as data, and each call is shown in the activity panel.

## Running it

It starts with the rest of the stack (`make up`), from the `mcp` target of the main [Dockerfile](../Dockerfile). The API finds it through `MCP_URL` (`http://mcp:8200/mcp` in Docker Compose). On its own:

```bash
uv run python -m mcp_server      # streamable HTTP on http://127.0.0.1:8200/mcp
```

With `MCP_URL` empty, enterprise-data questions are answered from the documents, and the answer says the tools were not connected.

## Tests

- `tests/integration/test_mcp_server.py`: the tools through the real MCP client in memory, their schemas, bad arguments refused by the server and by the API's own check, the data's consistency, a server that is down.
- `tests/graph/test_tools_agent.py`: the tools agent in the graph: live data in the answer, bad arguments from the model refused, the server down (documents only, with a limit), a viewer never reaching the tools, and the agent's own role check.
