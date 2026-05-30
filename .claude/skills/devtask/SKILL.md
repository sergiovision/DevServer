---
name: devtask
description: Generate a structured task JSON from a plain-text description. Returns JSON matching the DevServer tasks table schema, ready for INSERT via the API.
argument-hint: <task description>
allowed-tools:
disable-model-invocation: false
---

# DevTask - Generate Task JSON

You receive a free-form task description in `$ARGUMENTS`. Return **only** a single JSON object (no markdown fences, no explanation) that can be POSTed to `POST /api/tasks`.

**Do NOT explore or read the target repo.** The executing agent has full repo access — your job is to write a clear, compact spec from the user's description alone. Save tokens.

## Output Schema

```json
{
  "task_key": "<string>",
  "title": "<string>",
  "description": "<string>",
  "acceptance": "<string|null>",
  "priority": <1|2|3|4>,
  "labels": ["<string>", ...],
  "mode": "autonomous",
  "claude_mode": "api",
  "claude_model": "<string|null>",
  "max_turns": <number|null>,
  "skip_verify": false
}
```

`repo_id` is omitted — the caller supplies it.

## Field Rules

- **task_key**: UPPERCASE hyphenated slug, `AREA-SHORT-SLUG` format, max 64 chars.
- **title**: Imperative, under 100 chars.
- **description**: Concise actionable spec. State what to do, not how to find it. The executing agent will explore the repo itself — don't guess file paths or implementation details you don't know. Include edge cases only if obvious from the request.
- **acceptance**: Bullet list of testable criteria, or `null` if trivial. Keep short — 2-5 bullets max.
- **priority**: 1=critical/prod-down, 2=high/blocking, 3=medium (default), 4=low/cosmetic.
- **labels**: 1-3 lowercase kebab-case. Common: `feature`, `bug`, `refactor`, `ui`, `api`, `database`, `docs`, `security`, `performance`, `devops`.
- **claude_model**: `null` (repo default) for most tasks. `"claude-sonnet-4-20250514"` for simple/well-defined. `"claude-opus-4-20250514"` for complex/multi-file/security.
- **max_turns**: Simple fix: 10-20. Small feature: 20-40. Medium: 40-60. Large: 60-100. Open-ended: `null`.
- **mode**: Always `"autonomous"` unless explicitly interactive.
- **claude_mode**: Always `"api"` unless user says Max.
- **skip_verify**: Always `false` unless user says skip.

## Rules

1. Output ONLY valid JSON. No fences, no explanation.
2. Do NOT include system fields: `repo_id`, `id`, `status`, `created_by`, `created_at`, `updated_at`, `depends_on`, `bullmq_job_id`.
3. Never invent requirements not implied by the description.
4. If description is too vague for acceptance criteria, set `acceptance` to `null`.
