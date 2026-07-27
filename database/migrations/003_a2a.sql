-- 003 — A2A 1.0 peer gateway (Pro)
--
-- Exposes DevServer as an Agent2Agent (A2A) peer so external agents can
-- discover it via an Agent Card and delegate long-running coding work to it
-- over JSON-RPC 2.0. See ``docs/PlanA2A.md`` for the full design.
--
-- The gateway lives entirely in the Next.js tier (``app/api/pro/a2a``) — the
-- worker is deliberately NOT exposed, because it has no authentication and
-- serves ``/internal/env`` (cleartext secrets) on the same flat namespace.
--
-- Idempotent: safe to re-run on an existing database.

-- ─── A2A peers ───────────────────────────────────────────────────────────────
-- One row per external agent allowed to talk to this DevServer. The row is
-- both the credential AND the authorization scope: what repos it may touch,
-- what task types it may create, and the governance ceilings applied to every
-- task it spawns.
--
-- Defaults are deliberately the safest possible combination: no repo access,
-- research-only, human plan approval required, patch-only git flow. Widening
-- a peer's scope is an explicit operator action.
CREATE TABLE IF NOT EXISTS a2a_peers (
    id                    SERIAL PRIMARY KEY,
    name                  VARCHAR(128) NOT NULL,
    slug                  VARCHAR(64)  UNIQUE NOT NULL,   -- audit tag: created_by = 'a2a:<slug>'

    -- Credential. Only the SHA-256 of the bearer token is stored; the
    -- plaintext is shown exactly once at creation and is unrecoverable
    -- afterwards. (webhook_triggers.secret stores plaintext — this is the
    -- hardened version of that pattern.)
    token_sha256          VARCHAR(64)  UNIQUE NOT NULL,

    -- ─── Authorization scope ───
    allowed_repo_ids      INTEGER[]    NOT NULL DEFAULT '{}',          -- empty = no repo access at all
    allowed_task_types    TEXT[]       NOT NULL DEFAULT '{research}',  -- subset of coding|test|skill|script|research

    -- ─── Governance ceilings applied to every task this peer creates ───
    require_plan_approval BOOLEAN      NOT NULL DEFAULT TRUE,   -- forces mode='interactive'
    force_git_flow        VARCHAR(16)  NOT NULL DEFAULT 'patch',-- 'patch' never pushes
    max_cost_usd          NUMERIC(10,4),                        -- NULL = no ceiling
    max_wall_seconds      INTEGER,                              -- NULL = no ceiling
    default_agent_vendor  VARCHAR(16)  NOT NULL DEFAULT 'anthropic',
    default_model         VARCHAR(64),
    task_key_prefix       VARCHAR(16)  NOT NULL DEFAULT 'A2A',
    priority              INTEGER      NOT NULL DEFAULT 3,

    -- ─── Quota (the webhook path has none — a captured request is replayable
    --     indefinitely there; here every task creation decrements a budget) ───
    daily_task_quota      INTEGER      NOT NULL DEFAULT 10,
    tasks_today           INTEGER      NOT NULL DEFAULT 0,
    quota_reset_on        DATE         NOT NULL DEFAULT CURRENT_DATE,

    -- ─── Lifecycle / observability ───
    enabled               BOOLEAN      NOT NULL DEFAULT TRUE,
    last_seen_at          TIMESTAMPTZ,
    call_count            BIGINT       NOT NULL DEFAULT 0,
    task_count            BIGINT       NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Auth is a single indexed lookup on the token hash.
CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_peers_token
    ON a2a_peers (token_sha256);

-- ─── Push notification configs (A2A tasks/pushNotificationConfig/*) ──────────
-- Forward-declared for the phase-6 delivery backend. Creating the table now
-- keeps the migration count down and is harmless while unused.
CREATE TABLE IF NOT EXISTS a2a_push_configs (
    id           SERIAL PRIMARY KEY,
    config_id    VARCHAR(64)  NOT NULL,                 -- client-supplied or generated UUID
    peer_id      INTEGER      NOT NULL REFERENCES a2a_peers(id) ON DELETE CASCADE,
    task_id      INTEGER      NOT NULL REFERENCES tasks(id)     ON DELETE CASCADE,
    url          VARCHAR(1024) NOT NULL,                -- peer's webhook endpoint
    token        VARCHAR(256),                          -- opaque token echoed back to the peer
    auth_scheme  VARCHAR(32),                           -- 'bearer' | 'apikey' | NULL
    auth_secret  VARCHAR(512),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (task_id, config_id)
);

CREATE INDEX IF NOT EXISTS idx_a2a_push_configs_task
    ON a2a_push_configs (task_id);

-- ─── Scoping index ───────────────────────────────────────────────────────────
-- ``tasks/list`` filters on created_by = 'a2a:<slug>' so a peer only ever sees
-- the tasks it created. Without this the scoping filter is a seq scan.
CREATE INDEX IF NOT EXISTS idx_tasks_created_by
    ON tasks (created_by);
