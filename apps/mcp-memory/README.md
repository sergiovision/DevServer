# devserver-mcp

A tiny, provider-agnostic **stdio MCP server** that gives an interactive coding
CLI (Claude Code / Codex / Gemini) a **task-control lane** into DevServer — create
a task in a repo with a chosen vendor/model, start it on the local worker, then
read its live state and final output. It owns no database — every tool is a
thin `httpx` proxy to the worker's `/internal/tasks/*` endpoints.

> This server gives your CLI the task queue and this DevServer's repo knowledge base. [DevAgentForce](https://github.com/sergiovision/DevAgentForce) is the separate product DevServer grew into; DevServer itself is a single open-source edition with no Pro tier.

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
| `DEVSERVER_MCP_TRANSPORT` | `stdio` | `http` (also `streamable-http` / `sse`) switches to remote mode |
| `DEVSERVER_MCP_HOST` | `127.0.0.1` | Remote mode bind address |
| `DEVSERVER_MCP_PORT` | `8900` | Remote mode port |
| `DEVSERVER_WEB_URL` | `http://localhost:3200` | Next.js tier, used in remote mode for token introspection and the A2A task gateway |

## Remote mode (HTTP)

`DEVSERVER_MCP_TRANSPORT=http` serves Streamable HTTP on
`DEVSERVER_MCP_HOST:DEVSERVER_MCP_PORT` (default `127.0.0.1:8900`) so an
off-box agent can connect:

- **Auth**: every request needs `Authorization: Bearer <token>` with a DevServer
  **A2A peer token** (create the peer under `/a2a` in the dashboard). Tokens are
  introspected through `DEVSERVER_WEB_URL` (cached ~30 s, so revocation takes
  effect within that window).
- **Scope**: each call is limited to the peer's allowed repos; task tools go
  through the governed A2A gateway (quota, task types, plan approval).
  `task_run`, `propose_action` and `corpus_search_all` are refused remotely.
- **TLS**: the server speaks **plain HTTP**. Whenever the host is not
  localhost, put it behind a TLS-terminating reverse proxy — the bearer token
  travels in every request.
- Expose this server, **never** the worker (port 8000 has no authentication).

## Tools

`task_options` (valid vendors/models/task types), `task_create` (create + start),
`task_run` (start/re-run an existing task), `task_list`, `task_status` (state +
latest run + PR url), `task_output` (tail the deliverable/log), `task_cancel`.

Repo knowledge base: `wake_digest`, `memory_recall`, `memory_facts`,
`memory_decisions`, `memory_transcripts`, `repo_map`, `code_search`,
`doc_search`, `corpus_stats`, `corpus_ingest`, `corpus_search_all`,
`memory_remember`, `record_fact`, `record_decision`.

Ask Agent only: `propose_action` (queues an action card the user must approve).

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
