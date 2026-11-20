# ADR-0012: draw.io for architecture pictures, Mermaid for flows, agent graph from code

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

Reviewers need to open the diagrams straight from the README, the diagrams must be easy to walk through in the demo video, and they must stay true to the code as it changes.

## Decision

- **Architecture pictures** (context, containers, agent graph design, security layers, failure handling): **draw.io**, saved as `*.drawio.svg` in `docs/architecture/`. GitHub shows them as images, and the same file opens for editing in the draw.io desktop app or the VS Code *Draw.io Integration* extension.
- **Flows and sequences** (search pipeline, RLM, memory, streaming, request lifecycle): **Mermaid** in Markdown, rendered by GitHub and easy to diff.
- **Agent graph from code:** `make diagrams` exports the compiled LangGraph with `get_graph().draw_mermaid()` (added with the agents branch), so the implementation can be compared with the design view.
- The views follow the **C4 model** (context, containers, components).

## Consequences

- **Positive:** no image exports to keep in sync; diagrams change in the same commits as the code they describe.
- **Negative:** two diagram tools. Each is used for what it does best, and both are free.

## Alternatives considered

| Option | Why not |
|---|---|
| Excalidraw | Hand-drawn look is less suited to a bank architecture review |
| PlantUML, D2, Structurizr | Not rendered natively by GitHub, or extra tooling |
| Lucidchart | Limited free tier; files not in the repo |

## Related

[docs/architecture](../architecture/README.md)
