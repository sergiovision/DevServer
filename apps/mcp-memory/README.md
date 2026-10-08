# devserver-mcp

A tiny, provider-agnostic **stdio MCP server** that gives an interactive coding
CLI (Claude Code / Codex / Gemini) a **task-control lane** into DevServer — create
a task in a repo with a chosen vendor/model, start it on the local worker, then
read its live state and final output. It owns no database — every tool is a
thin `httpx` proxy to the worker's `/internal/tasks/*` endpoints.

## Install

```bash
cd apps/mcp-memory
uv sync            # or: pip install -e .
```

Requires a running DevServer worker reachable at `DEVSERVER_WORKER_URL`.

## Configure your CLI

All three CLIs launch the **same** command with cwd = your project, so repo
auto-detection (for `task_create` without a `repo_id`) works identically.

**Claude Code** — project `.mcp.json` (or `claude mcp add`):
```json
{
  "mcpServers": {
    "DevServer": {
      "command": "uv",
      "args": ["--directory", "/path/to/DevServer/apps/mcp-memory", "run", "devserver-mcp"],
      "env": { "DEVSERVER_WORKER_URL": "http://localhost:8000", "DEVSERVER_AGENT": "claude" }
    }
  }
}
```

**Codex** — `~/.codex/config.toml`:
```toml
[mcp_servers.DevServer]
command = "uv"
args = ["--directory", "/path/to/DevServer/apps/mcp-memory", "run", "devserver-mcp"]
env = { DEVSERVER_WORKER_URL = "http://localhost:8000", DEVSERVER_AGENT = "codex" }
```

**Gemini CLI** — `.gemini/settings.json`:
```json
{ "mcpServers": { "DevServer": {
  "command": "uv", "args": ["--directory", "/path/to/DevServer/apps/mcp-memory", "run", "devserver-mcp"],
  "env": { "DEVSERVER_WORKER_URL": "http://localhost:8000", "DEVSERVER_AGENT": "gemini" }
} } }
```

## Environment

| Var | Default | Purpose |
|---|---|---|
| `DEVSERVER_WORKER_URL` | `http://localhost:8000` | Worker base URL |
| `DEVSERVER_TOKEN` | — | Bearer token for a remote (non-localhost) worker |
| `DEVSERVER_AGENT` | — | CLI name recorded as `created_by=mcp:<agent>` on new tasks |

## Tools

`task_options` (valid vendors/models/task types), `task_create` (create + start),
`task_run` (start/re-run an existing task), `task_list`, `task_status` (state +
latest run + PR url), `task_output` (tail the deliverable/log), `task_cancel`.

Typical flow: `task_options` → `task_create(title=…, description=…,
task_type="coding", agent_vendor="anthropic", claude_model=…)` (repo defaults to
the current folder; `enqueue=true` starts it immediately) → poll
`task_status(task_key)` until `done`/`failed` → `task_output(task_key)` for the
result. `skill`/`research` tasks need no repo; their answer lands in
`task_output`.

Enqueue always flows through the Next.js queue producer (the single source of
truth for the job queue), so both the worker and web app must be running for
`task_create(enqueue=true)` / `task_run` to actually start a task.

## Graceful degradation

If the worker is down or returns an error, tools return `{"error": "…"}` rather
than failing — so the model can report the problem and carry on.
