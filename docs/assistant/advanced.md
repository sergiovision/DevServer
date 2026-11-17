# Advanced features

DevServer is MIT-licensed and ships as one edition: every feature below is
included and free. There is no licence key, no trial and nothing to activate.
Some features are opt-in per task or per repository, so they stay out of the
way until you need them.

## Governance and safety

- **Reality gate** — scores a task 0–100 on evidence before spending an agent
  run, plus a **strict abstain gate** that blocks tasks below your threshold.
- **Interactive plan gate** — set a task's mode to *interactive* and the agent
  proposes a plan first and waits for your approval; the approved file list
  then becomes an enforced allow-list.
- **Budget circuit breaker** — per-task cost and wall-clock ceilings that block
  rather than fail.
- **PR preflight** — deterministic secret scan, author check, file-size check
  and scope-creep detection before anything is pushed.
- **Patch export** — `git format-patch` output and a downloadable
  `combined.mbox` for applying the same change to another repo.

## Memory and search

- **Per-repo memory knowledge base** — pgvector recall, hybrid vector+lexical
  search, verbatim transcript drawers, temporal facts, decision memory, a
  wake-up digest, and cross-repo memory tunnels. All embeddings are computed
  locally; no cloud key.
- **Semantic code and doc search** over each repo's actual files — rebuild it
  with **Reindex** on the repository page.

## Automation and integration

- **Night cycle** — an overnight batch runner for queued work.
- **Rich Telegram** — inline approve/reject keyboards and a daily digest.
- **Inter-task messaging** — agents can message each other and you.
- **Webhook triggers** (sidebar → **Webhooks**, `/webhooks`) — Gitea, GitHub,
  Sentry and Grafana events create tasks.
- **A2A gateway** (sidebar → **Agent Peers**, `/a2a`) — DevServer as a
  governed, agent-reachable delegate for external agents.
- **Sessions** (sidebar → **Sessions**, `/sessions`) — browse the Claude Code
  and Codex CLI sessions on this machine.

## Ask Agent with tools

When the System LLM vendor is Anthropic or GLM, this panel runs as an agent:
it can read the repository you are asking about, check live task state through
the DevServer MCP server, and propose fixes — restarting a stuck database,
applying migrations, taking a backup, creating a task — which you approve with
one click. Other vendors answer from this handbook only. See "About this
assistant" for the details.
