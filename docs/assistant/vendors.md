# Agents, models and billing

Every task runs on a coding-agent CLI. DevServer supports four vendors, chosen
per task on the **Vendor | Model | Billing** row of the task form.

| Vendor | CLI | Notes |
|---|---|---|
| **Anthropic** | `claude` | Claude Code. The most exercised path. |
| **Google** | `agy` | Antigravity CLI. Model names carry the effort level, e.g. `gemini-3.1-pro-high`. |
| **OpenAI** | `codex` | |
| **GLM** | `claude` | Zhipu — the real Claude binary pointed at Zhipu's Anthropic-compatible endpoint. |

Changing the vendor resets the model to that vendor's default. The CLI for the
vendor you pick has to be installed on the worker machine; if it is missing, the
task fails immediately with a *"CLI not found"* message rather than hanging.

## Billing modes

- **Max (subscription)** — DevServer strips the vendor's API key from the CLI's
  environment, so the CLI falls back to the OAuth login you set up
  interactively on the host (Claude Max, ChatGPT Plus, Google AI Pro). Flat fee,
  no per-token metering. This is the usual choice.
- **API Platform** — the CLI inherits your API key and bills per token.

GLM always uses its API key: the GLM launcher is Claude Code redirected to
Zhipu and has no separate subscription login.

Cost tracking only means something in API mode — a Max subscription reports
zero cost per call, so the dashboard's cost charts will read `$0` for those.

## Backup model and auto-failover

Each task can carry a **backup vendor + model**. When the primary exhausts all
its retries — including rate-limit backoff — DevServer switches to the backup
and runs a fresh retry loop: new session, committed work preserved, error
history cleared. If Anthropic is rate-limited, the task can transparently
continue on GLM or Google.

Leave the backup vendor unset for a same-vendor model downgrade (Opus →
Sonnet); set a *different* vendor for true cross-vendor failover.

## Rate limits

A 429 is retried inside the same call with backoff (30s, 60s, 120s plus jitter)
rather than burning a task-level attempt. You will see `rate_limit_backoff`
events on the task timeline while that happens. Running many tasks
concurrently against one vendor is the usual cause.

## The System LLM is separate

The vendor/model on a *task* is the agent that writes code. The **System LLM**
in Settings is a different, cheaper setting used for DevServer's own small
calls — Fill Task, DevPlan, context compaction, and this assistant. It has its
own vendor, model and billing mode.
