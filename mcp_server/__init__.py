"""The MCP server with Helix Bank's (fictional) enterprise data.

Three read-only tools over the employee directory, the service catalog and
the incident records (data/enterprise.json). The API's tools agent calls
them for analysts and admins; nothing else can reach the server (see
README.md in this folder).

This package imports nothing from the agents, the API or search; it only
uses `core` for logging. import-linter checks the rule.
"""
