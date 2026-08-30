"""SQLite schema foundation for durable side-effect + protected-state persistence.

Schema v1: side-effect executions, idempotency, reconciliations (P7D).
Schema v2: additive HITL approvals, execution permits, workflow runtime (P7E).
Schema v3: first-class tenant_id on workflow_runtime_state + index/backfill.
Schema v4: durable workflow_schedules (schedule definitions survive restart).
Schema v5: durable task queue (atomic claim / lease / heartbeat).
Schema v6: schedule window claim columns (multi-process scheduler safety).
Schema v7: queue execution_lane + provider governor tables (Block 2).
Schema v8: first-class tenant_id on side_effect_executions + backfill (P1-SE-TENANT).
"""

SCHEMA_VERSION = 8
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7, 8})
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
    encrypted_payload_json TEXT,
    tenant_id TEXT NOT NULL DEFAULT ''
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

# Additive P7E tables. Never DROP. Safe to re-run (IF NOT EXISTS).
DDL_V2 = """
CREATE TABLE IF NOT EXISTS workflow_runtime_state (
    workflow_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    current_step TEXT,
    waiting_reason TEXT,
    approval_id TEXT,
    action_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    error_code TEXT,
    execution_key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
    encrypted_payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_wf_runtime_state
    ON workflow_runtime_state(state);
CREATE INDEX IF NOT EXISTS idx_wf_runtime_task
    ON workflow_runtime_state(task_id);
CREATE INDEX IF NOT EXISTS idx_wf_runtime_execution_key
    ON workflow_runtime_state(execution_key);

CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    workflow_id TEXT PRIMARY KEY,
    workflow_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    current_step TEXT,
    completed_steps_json TEXT NOT NULL DEFAULT '[]',
    timestamp TEXT NOT NULL,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
    encrypted_payload_json TEXT
);

CREATE TABLE IF NOT EXISTS hitl_approvals (
    approval_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    reason_code TEXT,
    approval_class TEXT NOT NULL DEFAULT 'standard',
    requested_by TEXT NOT NULL DEFAULT '',
    resolved_by TEXT,
    requested_at TEXT,
    expires_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    action_fingerprint TEXT NOT NULL DEFAULT '',
    required_approvals INTEGER NOT NULL DEFAULT 1,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
    encrypted_payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_hitl_approval_status
    ON hitl_approvals(status);
CREATE INDEX IF NOT EXISTS idx_hitl_approval_workflow
    ON hitl_approvals(workflow_id);
CREATE INDEX IF NOT EXISTS idx_hitl_approval_action
    ON hitl_approvals(action_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hitl_approval_pending_action
    ON hitl_approvals(action_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS execution_permits (
    permit_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    action_fingerprint TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    tool_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT,
    single_use INTEGER NOT NULL DEFAULT 1,
    sensitivity TEXT NOT NULL DEFAULT 'internal',
    safe_metadata_json TEXT NOT NULL DEFAULT '{}',
    encrypted_payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_permit_status ON execution_permits(status);
CREATE INDEX IF NOT EXISTS idx_permit_approval ON execution_permits(approval_id);
CREATE INDEX IF NOT EXISTS idx_permit_workflow ON execution_permits(workflow_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_permit_active_approval
    ON execution_permits(approval_id) WHERE status = 'issued';
"""

# Additive v3: first-class tenant on workflow runtime (ALTER + index + backfill in code).
DDL_V3_INDEX = """
CREATE INDEX IF NOT EXISTS idx_wf_runtime_tenant
    ON workflow_runtime_state(tenant_id);
"""

# Additive v4: durable workflow schedules (no second scheduler).
DDL_V4 = """
CREATE TABLE IF NOT EXISTS workflow_schedules (
    schedule_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workflow_type TEXT NOT NULL,
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    next_run_at TEXT NOT NULL,
    interval_seconds REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_enqueued_at TEXT,
    last_execution_key TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_wf_schedules_due
    ON workflow_schedules(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_wf_schedules_tenant
    ON workflow_schedules(tenant_id);
"""

# Additive v5: durable task queue for multi-process claim/lease.
DDL_V5 = """
CREATE TABLE IF NOT EXISTS queue_tasks (
    queue_task_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    actor_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    timeout_seconds REAL,
    error_code TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    worker_id TEXT,
    lease_id TEXT,
    leased_at TEXT,
    lease_expires_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_queue_ready
    ON queue_tasks(status, available_at);
CREATE INDEX IF NOT EXISTS idx_queue_execution_key
    ON queue_tasks(execution_key);
CREATE INDEX IF NOT EXISTS idx_queue_lease
    ON queue_tasks(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_queue_workflow
    ON queue_tasks(workflow_id);
CREATE INDEX IF NOT EXISTS idx_queue_tenant
    ON queue_tasks(tenant_id);
"""

# v6 schedule claim columns are applied in code (IF NOT EXISTS column check).
DDL_V6_COLUMNS = (
    ("claim_token", "TEXT"),
    ("claim_until", "TEXT"),
    ("claimed_window_at", "TEXT"),
)

# v7: first-class execution lane on queue + shared provider governor.
DDL_V7_COLUMNS = (("execution_lane", "TEXT NOT NULL DEFAULT 'background'"),)

DDL_V7 = """
CREATE INDEX IF NOT EXISTS idx_queue_lane
    ON queue_tasks(execution_lane, status, available_at);

CREATE TABLE IF NOT EXISTS provider_governor_slots (
    slot_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    lane TEXT NOT NULL DEFAULT 'background',
    worker_id TEXT,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gov_slots_provider
    ON provider_governor_slots(provider_id, model_id, expires_at);

CREATE TABLE IF NOT EXISTS provider_governor_state (
    state_key TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    breaker_state TEXT NOT NULL DEFAULT 'CLOSED',
    failure_count INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT,
    half_open_probes INTEGER NOT NULL DEFAULT 0,
    throttle_until TEXT,
    rpm_window_start TEXT,
    rpm_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

EXECUTION_LINKAGE_COLUMNS = (
    ("permit_id", "TEXT"),
    ("approval_id", "TEXT"),
)

# v8: first-class tenant on side_effect_executions (nullable during backfill; app fail-closed on new writes).
DDL_V8_COLUMNS = (("tenant_id", "TEXT NOT NULL DEFAULT ''"),)
DDL_V8_INDEX = """
CREATE INDEX IF NOT EXISTS idx_se_exec_tenant
    ON side_effect_executions(tenant_id);
"""
