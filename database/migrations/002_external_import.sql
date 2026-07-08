-- 002 — External import framework (Confluence first)
--
-- Adds the pluggable external-import substrate: an ``external_imports``
-- history/idempotency table that maps a source document (Confluence page,
-- future Jira issue, …) to the DevServer entity it was imported as, plus
-- per-repo Confluence connection overrides on ``repos``.
--
-- Idempotent: safe to re-run on an existing database.

-- ─── External imports ────────────────────────────────────────────────────────
-- One row per (source document → target entity) import. Re-importing the same
-- document as the same target type finds this row: if ``external_rev`` (e.g.
-- the Confluence version number) is unchanged the import is skipped, otherwise
-- the target is updated in place — never duplicated.
CREATE TABLE IF NOT EXISTS external_imports (
    id           SERIAL PRIMARY KEY,
    source       VARCHAR(64)   NOT NULL,              -- 'confluence', …
    external_id  VARCHAR(128)  NOT NULL,              -- source-native document id
    external_rev VARCHAR(128)  DEFAULT '',            -- source version/revision marker
    scope        VARCHAR(256)  DEFAULT '',            -- source container (space key, …)
    target_type  VARCHAR(64)   NOT NULL,              -- 'task' | 'idea'
    target_id    INTEGER,                             -- tasks.id / ideas.id (no FK: targets are deletable)
    title        VARCHAR(512)  DEFAULT '',
    source_url   VARCHAR(1024) DEFAULT '',
    imported_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (source, external_id, target_type)
);

CREATE INDEX IF NOT EXISTS idx_external_imports_source
    ON external_imports (source, scope);

-- ─── Per-repo Confluence overrides ───────────────────────────────────────────
-- Blank = fall back to the global CONFLUENCE_* env configuration. A repo that
-- sets these talks to its own Confluence instance/credentials.
ALTER TABLE repos ADD COLUMN IF NOT EXISTS confluence_url      VARCHAR(512) DEFAULT '';
ALTER TABLE repos ADD COLUMN IF NOT EXISTS confluence_username VARCHAR(256) DEFAULT '';
ALTER TABLE repos ADD COLUMN IF NOT EXISTS confluence_token    VARCHAR(512) DEFAULT '';
