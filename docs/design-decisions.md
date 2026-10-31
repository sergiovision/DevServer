# Design Decisions

The reasoning behind the main shapes in the code. For the module-by-module walkthrough see [architecture.md](architecture.md). Back to the [README](../README.md).

## 1. Multi-vendor agent backends

DevServer isn't locked to one AI provider. The `AgentBackend` abstraction covers four vendors out of the box:

| Vendor | CLI Binary | Picker models (default first) | Source of the IDs |
|---|---|---|---|
| `anthropic` | `claude` | **Claude Sonnet 5.5**, Opus 5.5, Fable 5.1, **Haiku 5.5**, Sonnet 5, Opus 5, Fable 5 | Anthropic models page |
| `google` | `agy` | **Gemini 3.1 Pro** (High/Low), **Gemini 4 Argon** (High/Medium/Low), Gemini 3.8 Flash | `agy models` (Antigravity CLI 1.4.9) |
| `openai` | `codex` | **GPT-6 Astra**, GPT-5.6 Sol / Terra / Luna, GPT-5.5 | model catalogue in codex-cli 0.155.1 |
| `glm` | `claude` | **GLM-5.3**, 5.2, 5.1, 5 Turbo, 5 — the Claude CLI against Zhipu's Anthropic-compatible API | Zhipu `/models` API |

Older models (Claude 4.x, Gemini 3.6/3.7 Flash, GPT-5.2–5.4, GLM-4.x) are marked `deprecated`: hidden from the pickers, still accepted where a task or repo already stores them.

Each task carries `agent_vendor`, `claude_model`, and `claude_mode` (billing mode: `api` or `max`). The worker dispatches to the right backend automatically. Adding a new vendor is one `AgentBackend` subclass, a `register_backend()` call and its models in `VENDOR_MODELS`.

> **Note — Google migrated to the Antigravity CLI.** Google retired the Gemini CLI on 2026-06-18 (`gemini` commands now return *410 Gone*). The Google backend now drives Google's replacement, the **Antigravity CLI** (`agy`), reusing the same Gemini API key and Google AI Pro/Ultra subscription. Install with `curl -fsSL https://antigravity.google/cli/install.sh | bash`.

**Billing modes are vendor-agnostic.** `api` inherits the vendor's API-key env var; `max` strips it so the CLI uses its own subscription login. That means you can run a task on **Claude Max**, **ChatGPT Plus** (`codex login`), or **Google AI Pro / Ultra** without per-token metering. For Google, sign in once with `agy` (the Google account holding the subscription), set the task's billing to **Max**, and pick `gemini-3.1-pro-high`. In `api` mode the worker reuses your existing `GEMINI_API_KEY` (bridged to `ANTIGRAVITY_API_KEY`).

📂 [`apps/worker/src/services/agent_backends.py`](../apps/worker/src/services/agent_backends.py)

## 2. Auto-failover between vendors

Each task can have a `backup_vendor` and `backup_model`. When the primary vendor exhausts all retries (including rate-limit backoff), the runner automatically switches:

- Resets to the backup vendor's `AgentBackend`
- Clears the session (sessions can't be resumed cross-vendor)
- Commits any in-progress work
- Runs a fresh retry loop with the backup vendor/model

This means a rate-limited Anthropic task can transparently continue on GLM or Google — no manual intervention.

## 3. Error-class-aware retries, not blanket re-runs

The naive "append stderr, retry" loop costs a full Claude session per attempt. DevServer runs verifier/agent output through 20 regex rules spanning Python, TypeScript / Node, C# / .NET, Rust, Go, Java, Git, and shell. Each matched rule produces a structured `ErrorClass(key, hint, severity)`:

- **`recoverable`** errors (import error, test failure, TS compile error) inject a surgical remediation hint into the next retry's prompt.
- **`hard`** errors (merge conflict, `git nothing to commit`, `command not found`, permission denied) escalate immediately — no more retries.
- A `recoverable` class that repeats across two attempts escalates too, on the theory that "same error twice" means the agent is stuck.

📂 [`apps/worker/src/services/error_classifier.py`](../apps/worker/src/services/error_classifier.py)

## 4. Multi-language repo map

Before any agent subprocess starts, the worker builds a regex-based symbol index covering classes, functions, and types across 11 languages. This ~4 KB block in the prompt eliminates "file not found" retries by giving the agent an accurate picture of what exists and where.

📂 [`apps/worker/src/services/repo_map.py`](../apps/worker/src/services/repo_map.py)

## 5. Rate-limit hardening

Concurrent tasks hitting vendor rate limits are handled at two levels:

1. **Per-subprocess 429 backoff** — a vendor 429 is retried by `retry_async` with a `Backoff((30, 60, 120), jitter=10)` policy without consuming a task-level attempt.
2. **Minimal retry prompts** — resumed sessions receive only the error/remediation block, not the full context again, cutting per-retry token usage by ~50%.

📂 [`runner/agent_call.py`](../apps/worker/src/services/runner/agent_call.py) · [`runner/prompt.py`](../apps/worker/src/services/runner/prompt.py)

## 6. Governance-grade OpenTelemetry (opt-in)

DevServer can export its entire task pipeline as OpenTelemetry spans to any OTLP collector (Langfuse, Grafana Tempo, Datadog, Jaeger). It's wired at the **single `store.emit_event` chokepoint** every task event already flows through, so one wrapper covers all ~16 event types: a per-task root span is opened at task start, each event becomes a span event, and the run closes at the terminal status. Beyond the standard GenAI attributes (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`), spans carry **DevServer-only governance attributes** — `devserver.reality_score`, `devserver.budget.cost_usd` / `.wall_seconds`, `devserver.preflight.secret_hits`, `devserver.error_class`, `devserver.vendor_failover`, `devserver.final_status` — the richest agent telemetry, in your own stack.

It is **fully opt-in and zero-overhead**: a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set *and* the optional `otel` extra is installed (`uv sync --extra otel`), so deployments that don't use it pay nothing.

📂 [`apps/worker/src/services/telemetry.py`](../apps/worker/src/services/telemetry.py)

## 7. Patterns, named

The code uses a handful of well-known patterns, each where it removes a real problem rather than for its own sake:

| Pattern | Where | Why it is there |
|---|---|---|
| **Strategy** | `AgentBackend` subclasses in [`agent_backends.py`](../apps/worker/src/services/agent_backends.py) | Each vendor CLI differs in argv, env, output and 429 shape; the runner never branches on vendor. |
| **Registry** | `register_backend()`, `VENDOR_MODELS`, `RunningTasks` | One lookup point: tests plug a fake backend in; cancel finds the live run. |
| **Context object** | `TaskContext` in [`runner/context.py`](../apps/worker/src/services/runner/context.py) | Phases share state explicitly instead of through 30 local variables in one function. |
| **Parameter object** | `AgentInvocation` in [`runner/agent_call.py`](../apps/worker/src/services/runner/agent_call.py) | Eleven arguments become one immutable, named value. |
| **Result type / state machine** | `Verdict` + `AttemptResult` in [`runner/results.py`](../apps/worker/src/services/runner/results.py) | Control flow is data: the loop and the outcome are `match` statements, not `break`/`continue` webs. |
| **Repository functions / unit of work** | [`runner/store.py`](../apps/worker/src/services/runner/store.py), [`repo_lock.py`](../apps/worker/src/services/repo_lock.py) | SQL stays out of orchestration; every write is one short transaction. |
| **Lease with heartbeat** | [`repo_lock.hold()`](../apps/worker/src/services/repo_lock.py) | A crashed worker's lock expires; a live one is never mistaken for stale. |
| **Circuit breaker** | [`budget.py`](../apps/worker/src/services/budget.py) | A runaway retry loop stops at a cost or wall-clock cap. |
| **Retry policy object** | `Backoff` + `retry_async` in [`retry.py`](../apps/worker/src/services/retry.py) | The schedule is data; the loop is written once. |
| **Fan-out dispatcher (observer)** | [`notify/`](../apps/worker/src/services/notify/) | Telegram, Discord and push backends receive typed events; a failing channel never blocks another. |
| **Adapter** | [`import_sources/`](../apps/worker/src/services/import_sources/) | Confluence is one pluggable source behind a common interface. |
| **Dependency injection** | webhook firing in [`apps/web/src/lib/webhooks/`](../apps/web/src/lib/webhooks/) | The HTTP handler is tested with a fake data-access object, no database. |

