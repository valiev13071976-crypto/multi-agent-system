"""SQLite schema foundation for durable side-effect persistence (schema v1)."""

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "./data/side_effects.sqlite3"
MAX_SAFE_METADATA_BYTES = 16_384
MAX_ENCRYPTED_PAYLOAD_BYTES = 65_536

SCHEMA_META_TABLE = "side_effect_schema_meta"

DDL = """
CREATE TABLE IF NOT EXISTS side_effect_schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS side_effect_executions (
    execution_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    resource_ref TEXT,
    status TEXT NOT NULL,
    outcome TEXT NOT NULL,
    authorization_type TEXT NOT NULL,
    authorization_id TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    external_reference TEXT,
    reversible INTEGER NOT NULL DEFAULT 0,
    rollback_reference TEXT,
    rollback_status TEXT NOT NULL,
    error_code TEXT,
    parent_execution_id TEXT,
    reconciliation_id TEXT,
    recovery_attempt INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
    encrypted_payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_se_exec_idempotency
    ON side_effect_executions(idempotency_key_hash);
CREATE INDEX IF NOT EXISTS idx_se_exec_workflow
    ON side_effect_executions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_se_exec_status
    ON side_effect_executions(status);

CREATE TABLE IF NOT EXISTS idempotency_records (
    key_hash TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    execution_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
    encrypted_payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_idem_state ON idempotency_records(state);

CREATE TABLE IF NOT EXISTS reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    last_checked_at TEXT,
    next_check_at TEXT,
    external_reference TEXT,
    reason_code TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    resolver_id TEXT,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
    encrypted_payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_recon_execution ON reconciliations(execution_id);
CREATE INDEX IF NOT EXISTS idx_recon_status ON reconciliations(status);
"""
