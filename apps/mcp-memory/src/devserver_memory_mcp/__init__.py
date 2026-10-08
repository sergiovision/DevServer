"""devserver-mcp — the ``DevServer`` MCP server: task control for coding CLIs.

A thin, provider-agnostic stdio MCP server that proxies the running DevServer
worker's ``/internal/tasks/*`` endpoints so Claude Code / Codex / Gemini can
create, start and monitor DevServer tasks.
"""

__version__ = "0.1.0"
