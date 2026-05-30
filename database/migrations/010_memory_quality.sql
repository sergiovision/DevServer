-- DevServer v2 — migration 010
-- Memory-quality + strict-abstain support. Ports ideas from
-- total-agent-memory (recency decay + archive) and braincore (strict
-- abstain gate). All new behaviour is OPT-IN via the settings keys seeded
-- at the bottom: every threshold defaults to 0 / false so an unconfigured
-- deployment behaves exactly as it did before this migration.
--
-- Usage: psql -U devserver -d devserver -f database/migrations/010_memory_quality.sql
--
-- Idempotent: safe to run on both fresh and existing databases.

-- ─── Feature 1: strict abstain gate (braincore) ────────────────────────────
-- When the reality gate scores a task below `reality_abstain_threshold`, the
-- worker blocks it before spending an agent run and records why here so the
-- dashboard can show "abstained: <reason>" instead of a silent block.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS abstain_reason TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reality_score  INT;

-- ─── Feature 3: memory decay + auto-archive (total-agent-memory) ───────────
-- `recall_count` / `last_recalled_at` let recall apply recency weighting and
-- let the archive sweep spare memories that are actually used. `archived_at`
-- soft-deletes stale, never-recalled rows so recall stays fast and relevant.
ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS archived_at      TIMESTAMPTZ;
ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ;
ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS recall_count     INT NOT NULL DEFAULT 0;

-- Partial index so the common "active memories for a repo" scan stays cheap
-- even after lots of rows have been archived.
CREATE INDEX IF NOT EXISTS idx_agent_memory_active
    ON agent_memory (repo_id) WHERE archived_at IS NULL;

-- ─── New settings keys (all opt-in, safe defaults) ─────────────────────────
-- reality_abstain_threshold : 0 disables the abstain gate; set e.g. 40 to
--     block tasks scoring below 40/100 on the reality gate.
-- memory_decay_half_life_days: 0 disables recency decay in recall; recommend
--     90 to halve a memory's effective score every 90 days.
-- memory_archive_days        : 0 disables the archive sweep; recommend 180 to
--     archive never-recalled memories older than 180 days.
-- memory_iterative_recall    : false keeps single-pass recall; true enables
--     LLM-planned multi-hop recall (uses the system LLM).
INSERT INTO settings (key, value) VALUES
    ('reality_abstain_threshold',  '0'),
    ('memory_decay_half_life_days', '0'),
    ('memory_archive_days',         '0'),
    ('memory_iterative_recall',     'false')
ON CONFLICT (key) DO NOTHING;
