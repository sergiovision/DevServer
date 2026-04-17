-- DevServer v2 — migration 008
-- Inter-task messaging, webhook triggers, and context-compaction support.
--
-- Usage: psql -U devserver -d devserver -f database/migrations/008_messaging_webhooks_compaction.sql
--
-- Idempotent: safe to run on both fresh and existing databases.

-- ─── Inter-task messaging ──────────────────────────────────────────────────
-- A small bus so one task can ask another task to do something, poll its
-- state, or hand off a result. Mirrors OpenClaw's ``sessions_send`` /
-- ``sessions_history`` pattern. A message is either between two concrete
-- tasks (from_task_id + to_task_id) or addressed to a human operator
-- (to_task_id IS NULL, to_task_key = 'operator'). The task_events trigger
-- converts every insert into a PG NOTIFY so the dashboard sees messages
-- live, no extra plumbing needed.
CREATE TABLE IF NOT EXISTS task_messages (
    id           BIGSERIAL PRIMARY KEY,
    from_task_id INT         REFERENCES tasks(id) ON DELETE SET NULL,
    to_task_id   INT         REFERENCES tasks(id) ON DELETE CASCADE,
    -- Destination key resolved at insert time so a dropped task still keeps its history.
    to_task_key  VARCHAR(64) NOT NULL,
    from_task_key VARCHAR(64),
    -- 'request' | 'response' | 'note' | 'handoff'
    kind         VARCHAR(16) NOT NULL DEFAULT 'note',
    subject      VARCHAR(256),
    body         TEXT        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}',
    read_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_messages_to_unread
    ON task_messages(to_task_id, created_at)
    WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_task_messages_from ON task_messages(from_task_id);
CREATE INDEX IF NOT EXISTS idx_task_messages_to_key ON task_messages(to_task_key);

-- PG NOTIFY on every insert so the dashboard can live-tail a task inbox.
CREATE OR REPLACE FUNCTION notify_task_message() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('task_messages', json_build_object(
        'id',           NEW.id,
        'from_task_id', NEW.from_task_id,
        'to_task_id',   NEW.to_task_id,
        'to_task_key',  NEW.to_task_key,
        'from_task_key', NEW.from_task_key,
        'kind',         NEW.kind,
        'subject',      NEW.subject,
        'created_at',   NEW.created_at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS task_message_notify ON task_messages;
CREATE TRIGGER task_message_notify
    AFTER INSERT ON task_messages
    FOR EACH ROW EXECUTE FUNCTION notify_task_message();


-- ─── Webhook triggers ──────────────────────────────────────────────────────
-- A webhook binds an opaque URL token to a repo + (optional) task template.
-- External systems (Gitea/GitHub webhooks, Sentry alerts, Grafana, cron
-- jobs) POST JSON at /api/webhooks/<token>. The handler validates the
-- optional HMAC secret, applies the template, and inserts a row in tasks.
CREATE TABLE IF NOT EXISTS webhook_triggers (
    id               SERIAL PRIMARY KEY,
    token            VARCHAR(64)  UNIQUE NOT NULL,
    name             VARCHAR(128) NOT NULL,
    repo_id          INT          REFERENCES repos(id) ON DELETE CASCADE NOT NULL,
    template_id      INT          REFERENCES task_templates(id) ON DELETE SET NULL,
    -- Optional HMAC-SHA256 secret. When set, incoming requests must
    -- present a matching ``X-DevServer-Signature`` header (sha256=HEX).
    secret           VARCHAR(128),
    -- Mustache-ish templates: ``{{json.path}}`` gets replaced with payload
    -- fields at fire time. Fallbacks used when not provided.
    title_template   TEXT         DEFAULT 'Webhook: {{name}}',
    description_template TEXT     DEFAULT '',
    task_key_prefix  VARCHAR(16)  DEFAULT 'HOOK',
    priority         INT          NOT NULL DEFAULT 3,
    enabled          BOOLEAN      NOT NULL DEFAULT TRUE,
    last_fired_at    TIMESTAMPTZ,
    fire_count       INT          NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_triggers_repo   ON webhook_triggers(repo_id);
CREATE INDEX IF NOT EXISTS idx_webhook_triggers_token  ON webhook_triggers(token);


-- ─── Context compaction ───────────────────────────────────────────────────
-- When a long-running task has burned through N retries on the same session
-- the agent_runner calls ``compaction.compact_task()`` which summarises the
-- accumulated transcript via the system LLM and stores the result here. On
-- the next retry the full Phase-1 context blocks are replaced by this
-- compacted summary, and session_id is cleared so the CLI starts fresh.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS compacted_context TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS compacted_at      TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS compact_count     INT NOT NULL DEFAULT 0;


-- ─── task_events: document new event types ────────────────────────────────
-- New event_type values (documentation only — the column is varchar):
--   'message_sent'        — a task sent a message via the messaging bus
--   'message_received'    — a task read its inbox and acknowledged N msgs
--   'webhook_triggered'   — a webhook token fired and created a task
--   'context_compacted'   — a long run was summarised and session reset
