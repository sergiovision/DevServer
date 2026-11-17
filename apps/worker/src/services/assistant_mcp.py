"""MCP wiring and the tool policy for the Ask Agent panel.

Gives the assistant's CLI subprocess exactly one MCP server — DevServer's own,
from ``apps/mcp-memory`` — and nothing else.

The tool policy here is the assistant's security boundary, and it is *not* the
same as the one autonomous task runs use. Three things were verified against
the installed CLI rather than assumed:

1. ``--dangerously-skip-permissions`` (which every task run passes) overrides
   ``--allowedTools`` entirely. A chat panel must never pass it.
2. ``--allowedTools`` on its own is an **auto-approve** list, not a restriction:
   with it set to ``Read,Glob`` the agent still invoked ``Bash``, and only the
   working-directory sandbox stopped the write. So an allow-list alone would
   have let a browser chat box run read-only shell commands.
3. ``--disallowedTools`` genuinely removes a tool — the model could not find
   ``Bash`` at all. That is the hard boundary, so both lists are always passed.

``--strict-mcp-config`` matters for the same reason: without it the subprocess
inherited MCP servers from the *operator's* own ``~/.claude.json`` during
testing. The assistant must see only what DevServer granted it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ASSISTANT_ALLOWED_TOOLS",
    "ASSISTANT_DISALLOWED_TOOLS",
    "ASSISTANT_EXTRA_ARGS",
    "resolve_mcp_command",
    "write_mcp_config",
]

#: Read-only lanes the assistant may use without asking. Side effects never go
#: through a tool — they go through ``propose_action`` and a human click.
ASSISTANT_ALLOWED_TOOLS = ",".join(
    [
        # DevServer memory + corpus (listed so they are usable once the MCP server
        # exposes them; an allow-list entry for an absent tool is inert)
        "mcp__DevServer__wake_digest",
        "mcp__DevServer__memory_recall",
        "mcp__DevServer__memory_facts",
        "mcp__DevServer__memory_decisions",
        "mcp__DevServer__memory_transcripts",
        "mcp__DevServer__code_search",
        "mcp__DevServer__doc_search",
        "mcp__DevServer__corpus_search_all",
        "mcp__DevServer__corpus_stats",
        "mcp__DevServer__repo_map",
        # Live task state
        "mcp__DevServer__task_list",
        "mcp__DevServer__task_status",
        "mcp__DevServer__task_output",
        "mcp__DevServer__task_options",
        # The one write lane, and it writes nothing: it records a proposal.
        "mcp__DevServer__propose_action",
        # Local read-only inspection of the repo the operator is asking about.
        "Read",
        "Glob",
        "Grep",
    ]
)

#: Hard-removed. Anything that could change state, spend money, or reach the
#: network without the operator seeing it. `Task` is here because a sub-agent
#: would not inherit this policy.
ASSISTANT_DISALLOWED_TOOLS = ",".join(
    [
        "Bash",
        "Write",
        "Edit",
        "NotebookEdit",
        "Task",
        "WebFetch",
        "WebSearch",
        "mcp__DevServer__task_create",
        "mcp__DevServer__task_run",
        "mcp__DevServer__task_cancel",
        "mcp__DevServer__corpus_ingest",
        "mcp__DevServer__memory_remember",
        "mcp__DevServer__record_fact",
        "mcp__DevServer__record_decision",
    ]
)

#: Passing --permission-mode is also what tells ClaudeBackend.build_command to
#: withhold --dangerously-skip-permissions.
ASSISTANT_EXTRA_ARGS = [
    "--permission-mode",
    "default",
    "--disallowedTools",
    ASSISTANT_DISALLOWED_TOOLS,
]

_CONFIG_NAME = ".devserver-mcp.json"


def _candidate_roots() -> list[Path]:
    here = Path(__file__).resolve()
    roots = []
    env_root = os.environ.get("DEVSERVER_ROOT")
    if env_root:
        roots.append(Path(env_root))
    roots.append(here.parents[4])  # apps/worker/src/services/… → repo root
    roots.append(here.parents[2])  # Docker: /app/src/services/… → /app
    return roots


def resolve_mcp_command() -> list[str] | None:
    """Locate a *runnable* ``devserver-mcp``, or return None if there isn't one.

    Order matters, and the last step is the subtle one. The MCP server is a
    separate uv project with its own dependencies (``fastmcp``), which the
    worker's virtualenv does not have — so pointing ``sys.executable`` at
    ``server.py`` produces a command that fails with ``ModuleNotFoundError``
    the moment the CLI spawns it, and the assistant silently loses every tool.
    We therefore only fall back to that form after checking the interpreter can
    actually import ``fastmcp``.
    """
    explicit = os.environ.get("DEVSERVER_MCP_BIN")
    if explicit:
        return [explicit]

    found = shutil.which("devserver-mcp")
    if found:
        return [found]

    # A host checkout normally has `uv sync`'d the MCP project in place.
    for root in _candidate_roots():
        console = root / "apps" / "mcp-memory" / ".venv" / "bin" / "devserver-mcp"
        if console.is_file() and os.access(console, os.X_OK):
            return [str(console)]
        win_console = root / "apps" / "mcp-memory" / ".venv" / "Scripts" / "devserver-mcp.exe"
        if win_console.is_file():
            return [str(win_console)]

    import importlib.util
    import sys

    if importlib.util.find_spec("fastmcp") is not None:
        for root in _candidate_roots():
            pkg = root / "apps" / "mcp-memory" / "src" / "devserver_memory_mcp" / "server.py"
            if pkg.is_file():
                return [sys.executable, str(pkg)]

    logger.warning(
        "devserver-mcp not found — the assistant will answer without DevServer "
        "tools. Install it with `uv sync` in apps/mcp-memory, or set "
        "DEVSERVER_MCP_BIN."
    )
    return None


def write_mcp_config(
    workdir: str,
    *,
    repo_id: int | None,
    session_id: str | None = None,
    agent_label: str = "assistant",
    worker_url: str | None = None,
) -> str | None:
    """Write the assistant's MCP config into ``workdir``; return its path.

    Returns None when the MCP server is not installed, in which case the caller
    runs without ``--mcp-config`` and the assistant degrades to local read-only
    tools plus the handbook.
    """
    command = resolve_mcp_command()
    if command is None:
        return None

    if not worker_url:
        from config import settings

        worker_url = f"http://127.0.0.1:{settings.worker_port}"

    env = {
        "DEVSERVER_WORKER_URL": worker_url,
        "DEVSERVER_AGENT": agent_label,
    }
    if repo_id is not None:
        env["DEVSERVER_REPO_ID"] = str(repo_id)
    if session_id:
        # Binds propose_action to this conversation, so the proposal surfaces in
        # the right panel and the right agent session gets resumed afterwards.
        env["DEVSERVER_ASSISTANT_SESSION"] = session_id
    # Deliberately no DEVSERVER_TOKEN / INTERNAL_API_TOKEN: this file lives in
    # the CLI's cwd, where the assistant's own Read tool could see it. The
    # action-recording endpoint (POST /internal/assistant/actions) only stores
    # a proposal and needs no token; executing one requires the operator's
    # token-guarded decide call from the web tier.

    config = {
        "mcpServers": {
            "DevServer": {
                "type": "stdio",
                "command": command[0],
                "args": command[1:],
                "env": env,
            }
        }
    }

    path = Path(workdir) / _CONFIG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write assistant MCP config: %s", exc)
        return None
    return str(path)
