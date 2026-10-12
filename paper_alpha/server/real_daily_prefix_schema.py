"""Immutable main-store source and OHLC-prefix schema; no import-time IO."""
SCHEMA_VERSION = 29
HANDLE_VERSION = "real-daily-source-handle-v1"
CAPTURE_VERSION = "real-daily-source-capture-v1"
NORMALIZATION_VERSION = "real-daily-main-normalization-v1"
REGISTRATION_VERSION = "real-daily-main-prefix-registration-v1"

TABLE_DDL = (
    "CREATE TABLE rdw_source_handles(id TEXT PRIMARY KEY,capture_id TEXT NOT NULL UNIQUE,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,handle_digest TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE rdw_normalization_intents(id TEXT PRIMARY KEY,reservation_id TEXT NOT NULL UNIQUE REFERENCES rdw_reservations(id),handle_id TEXT NOT NULL REFERENCES rdw_source_handles(id),approval_id TEXT NOT NULL REFERENCES rdw_approvals(id),owner_digest TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,intent_digest TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE rdw_normalization_outcomes(intent_id TEXT PRIMARY KEY REFERENCES rdw_normalization_intents(id),status TEXT NOT NULL CHECK(status IN ('completed','failed','interrupted')),outcome_digest TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE rdw_prefix_registrations(id TEXT PRIMARY KEY,intent_id TEXT NOT NULL UNIQUE REFERENCES rdw_normalization_intents(id),dataset_id TEXT NOT NULL UNIQUE REFERENCES datasets(id),registration_digest TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE rdw_revision_bindings(revision_id TEXT PRIMARY KEY REFERENCES revisions(id),registration_id TEXT NOT NULL REFERENCES rdw_prefix_registrations(id),assessment_id TEXT NOT NULL REFERENCES rdw_assessments(id),selection_digest TEXT NOT NULL,config_digest TEXT NOT NULL,task_digest TEXT NOT NULL,execution_digest TEXT NOT NULL,binding_digest TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE rdw_result_bindings(attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),run_id TEXT NOT NULL REFERENCES runs(id),revision_id TEXT NOT NULL REFERENCES revisions(id),registration_id TEXT NOT NULL REFERENCES rdw_prefix_registrations(id),producer_digest TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)",
)
REQUIRED_COLUMNS = {
    "rdw_source_handles": frozenset({"id", "capture_id", "idempotency_key", "request_digest", "handle_digest", "payload_json", "created_at"}),
    "rdw_normalization_intents": frozenset({"id", "reservation_id", "handle_id", "approval_id", "owner_digest", "idempotency_key", "request_digest", "intent_digest", "payload_json", "created_at"}),
    "rdw_normalization_outcomes": frozenset({"intent_id", "status", "outcome_digest", "payload_json", "created_at"}),
    "rdw_prefix_registrations": frozenset({"id", "intent_id", "dataset_id", "registration_digest", "payload_json", "created_at"}),
    "rdw_revision_bindings": frozenset({"revision_id", "registration_id", "assessment_id", "selection_digest", "config_digest", "task_digest", "execution_digest", "binding_digest", "payload_json", "created_at"}),
    "rdw_result_bindings": frozenset({"attempt_id", "run_id", "revision_id", "registration_id", "producer_digest", "payload_json", "created_at"}),
}
TRIGGER_DDL = tuple(
    "CREATE TRIGGER " + table + "_no_" + action.lower() + " BEFORE " + action +
    " ON " + table + " BEGIN SELECT RAISE(ABORT,'immutable real daily prefix'); END"
    for table in REQUIRED_COLUMNS for action in ("UPDATE", "DELETE")
)
SCHEMA = ";\n".join((*TABLE_DDL, *TRIGGER_DDL)) + ";\n"
