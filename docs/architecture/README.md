# Architecture diagrams

This folder holds every architecture diagram for the Enterprise Knowledge Agent. Start with the system context, then the containers diagram, then the more detailed views.

The diagrams follow the C4 model, a common way to describe software architecture at different levels of detail:

- **Context:** the system as one box, with its users and the outside systems it uses.
- **Containers:** the separate services that make up the system and how they talk to each other.
- **Components:** the main parts inside a service, such as the agents inside the backend.

The reasons behind the design are recorded in [docs/adr](../adr/README.md).

## Diagram list

| # | Diagram | Format | What it shows |
|---|---|---|---|
| 1 | [System context](01-system-context.drawio.svg) | draw.io | The three user roles, the assistant and every outside system it depends on |
| 2 | [Containers and deployment](02-containers.drawio.svg) | draw.io | The Docker Compose services, their ports and networks, and the cloud services they call |
| 3 | [Agent graph](03-agent-graph.drawio.svg) | draw.io | The supervisor and specialist agents, how work is routed, the approval step, answer checks, memory and the research sub-graph |
| 3a | [Agent graph, from the code](03-agent-graph.generated.md) | Mermaid (generated) | The graph as built today, drawn by `make diagrams`; a test fails if it falls behind the code |
| 4 | [Search pipeline](04-search-pipeline.md) | Mermaid | How documents are indexed, how a question is searched, and how the two search scores are combined |
| 5 | [Recursive research](05-rlm-research.md) | Mermaid | The six research steps, using the example "Summarize payment-failure outages over the last year" |
| 6 | [Layered security](06-security-layers.drawio.svg) | draw.io | The checks on every request and every answer, plus controls that apply everywhere |
| 7 | [Failure handling](07-failure-handling.drawio.svg) | draw.io | Switching to the backup language model, the circuit breaker, and what happens when other services fail |
| 8 | [Memory](08-memory.md) | Mermaid | Conversation memory, summaries, long-term memory per user, and the rules that protect it |
| 9 | [Streaming events](09-streaming-events.md) | Mermaid | The live events sent from the backend to the activity panel |
| 10 | [Request lifecycle](10-request-lifecycle.md) | Mermaid | One request from login to the final answer |
| - | [Test pyramid](test-pyramid.drawio.svg) | draw.io | The test layers, used in [TESTING.md](../TESTING.md) |

Diagram 3a is generated straight from the code (`make diagrams`). Comparing it with diagram 3 shows how much of the design is built: today the input guard, supervisor, retrieval agent, response agent and validator. The other agents appear in 3a as their branches add them.

## Colours in the draw.io diagrams

| Colour | Meaning |
|---|---|
| Blue | User interface |
| Green | Application and agents |
| Red | Security controls |
| Yellow | Data stores |
| Purple | Tools (MCP server, Python sandbox) |
| Grey | Outside and cloud services |
| Orange | Observability and the human approval step |

The system context diagram uses the standard C4 colours instead: dark blue for people, blue for this system and grey for outside systems.

## How to edit the diagrams

- **draw.io files (`*.drawio.svg`)** are normal SVG images with the editable diagram stored inside the file. GitHub shows them as images. To change one, open it in the draw.io desktop app or in VS Code with the *Draw.io Integration* extension, edit it, and save. The file stays a valid image.
- **Mermaid diagrams** are written as text inside the Markdown files. GitHub draws them automatically. Edit the text and the diagram updates.

When a feature changes the design, the diagrams are updated in the same branch.
