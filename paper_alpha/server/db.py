"""Durable single-host store. WAL and immediate transactions serialize transitions."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time

SCHEMA_VERSION = 17

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS papers(id TEXT PRIMARY KEY,title TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,document TEXT NOT NULL,pdf_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY,title TEXT NOT NULL,sha256 TEXT NOT NULL,metadata TEXT NOT NULL,data_path TEXT NOT NULL,metadata_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS researches(id TEXT PRIMARY KEY,title TEXT NOT NULL,paper_id TEXT NOT NULL REFERENCES papers(id),dataset_id TEXT NOT NULL REFERENCES datasets(id),created_at TEXT NOT NULL,latest_revision_id TEXT);
CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY,research_id TEXT NOT NULL REFERENCES researches(id),number INTEGER NOT NULL,task TEXT NOT NULL,note TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(research_id,number));
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,revision_id TEXT NOT NULL REFERENCES revisions(id),research_id TEXT NOT NULL REFERENCES researches(id),mode TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,error TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,worker_id TEXT,attempt_id TEXT,heartbeat REAL,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,verification TEXT,state TEXT);
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),number INTEGER NOT NULL,worker_id TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,error TEXT,output_dir TEXT NOT NULL,state_digest TEXT,verification TEXT,UNIQUE(run_id,number));
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES runs(id),attempt_id TEXT,kind TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),attempt_id TEXT NOT NULL REFERENCES attempts(id),name TEXT NOT NULL,path TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,UNIQUE(attempt_id,name));
CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),revision_id TEXT NOT NULL REFERENCES revisions(id),attempt_id TEXT NOT NULL REFERENCES attempts(id),candidate_id TEXT NOT NULL,verdict TEXT NOT NULL,category TEXT NOT NULL,note TEXT NOT NULL,result_digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS regression_cases(id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES reviews(id),research_id TEXT NOT NULL REFERENCES researches(id),paper_id TEXT NOT NULL REFERENCES papers(id),dataset_id TEXT NOT NULL REFERENCES datasets(id),candidate_id TEXT NOT NULL,expected_status TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,approved INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS regression_checks(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),created_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(status,created_at);
"""


def connect(path):
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _version(connection):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if not tables:
        return 0
    if 'settings' in tables:
        row = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        if row and row[0] in {'1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15', '16', '17'}:
            return int(row[0])
    raise RuntimeError('Unsupported workbench database schema; no migration was applied')


def _migrate_v1(connection):
    # Existing approvals remain historical status-only cases. They must be
    # explicitly approved again to acquire a scientific compatibility contract.
    connection.execute('ALTER TABLE regression_cases ADD COLUMN contract TEXT')
    connection.execute('ALTER TABLE regression_cases ADD COLUMN contract_digest TEXT')


def _migrate_v2(connection):
    from .dataset_imports import DATASET_IMPORT_SCHEMA
    for statement in DATASET_IMPORT_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)
    connection.execute("ALTER TABLE reviews ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy_unknown'")
    connection.execute('CREATE TABLE issues(id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES reviews(id),created_at TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL)')
    connection.execute('CREATE TABLE issue_events(id TEXT PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES issues(id),created_at TEXT NOT NULL,state TEXT NOT NULL,disposition TEXT NOT NULL,note TEXT NOT NULL,source TEXT NOT NULL,revision_id TEXT REFERENCES revisions(id),target_run_id TEXT REFERENCES runs(id),check_id TEXT REFERENCES regression_checks(id),idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,target_attempt_id TEXT REFERENCES attempts(id),target_result_digest TEXT)')
    connection.execute('CREATE INDEX issue_history ON issue_events(issue_id)')


def _migrate_v3(connection):
    # Historical generic verdicts are not retrospective semantic assessments.
    connection.execute('ALTER TABLE reviews ADD COLUMN assessment TEXT')


def _migrate_v4(connection):
    # No keys or receipts are invented for historical operations.
    from .mutations import LEGACY_RECEIPT_SCHEMA
    connection.execute(LEGACY_RECEIPT_SCHEMA)


def _migrate_v5(connection):
    from .mutations import RECEIPT_SCHEMA
    from .research_insights_schema import REPORT_SCHEMA
    # The existing CHECK constraint cannot be extended with ALTER COLUMN.
    # Rebuild in the migration transaction, copying response strings verbatim.
    connection.execute('ALTER TABLE mutation_receipts RENAME TO mutation_receipts_v5')
    connection.execute(RECEIPT_SCHEMA)
    connection.execute('INSERT INTO mutation_receipts(operation,idempotency_key,request_digest,response,response_digest,review_id,case_id,created_at) '
                       'SELECT operation,idempotency_key,request_digest,response,response_digest,review_id,case_id,created_at FROM mutation_receipts_v5')
    connection.execute('DROP TABLE mutation_receipts_v5')
    for statement in REPORT_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)


def _migrate_v6(connection):
    from .workflow_observations_schema import OBSERVATION_SCHEMA
    for statement in OBSERVATION_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)


def _migrate_v7(connection):
    from .mutations import WORKFLOW_RECEIPT_SCHEMA
    connection.execute(WORKFLOW_RECEIPT_SCHEMA)


def _migrate_v8(connection):
    # An additive resource preserves every historical task, receipt and output.
    connection.execute('CREATE TABLE research_protocols(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,parent_id TEXT REFERENCES research_protocols(id))')
    connection.execute('CREATE INDEX research_protocols_created ON research_protocols(created_at,id)')


def _migrate_v9(connection):
    from .monthly_storage_schema import SCHEMA as MONTHLY_SCHEMA
    for statement in MONTHLY_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)


def _migrate_v10(connection):
    from .author_panels import SCHEMA as AUTHOR_PANEL_SCHEMA
    for statement in AUTHOR_PANEL_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)


def _migrate_v11(connection):
    from .author_studies import SCHEMA as AUTHOR_STUDY_SCHEMA
    for statement in AUTHOR_STUDY_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)


def _migrate_v12(connection):
    from .research_cases import SCHEMA as CASE_SCHEMA
    from .research_tools import SCHEMA as TOOL_SCHEMA
    for statement in (CASE_SCHEMA + TOOL_SCHEMA).split(';'):
        if statement.strip():
            connection.execute(statement)


def _migrate_v13(connection):
    from .research_guard import SCHEMA as GUARD_SCHEMA, seed_history
    from .research_jobs import SCHEMA as JOB_SCHEMA
    from .research_claims import SCHEMA as CLAIM_SCHEMA
    for statement in (GUARD_SCHEMA + JOB_SCHEMA + CLAIM_SCHEMA).split(';'):
        if statement.strip():
            connection.execute(statement)
    seed_history(connection)


def _migrate_v14(connection):
    from .observation_identity import SCHEMA as OBSERVATION_SCHEMA, seed_history
    from .domain_research_jobs import SCHEMA as DOMAIN_JOB_SCHEMA
    from .semantic_annotations import SCHEMA as SEMANTIC_SCHEMA
    for statement in (OBSERVATION_SCHEMA + DOMAIN_JOB_SCHEMA + SEMANTIC_SCHEMA).split(';'):
        if statement.strip():
            connection.execute(statement)
    seed_history(connection)


def _migrate_v15(connection):
    from .claim_reviews import SCHEMA as CLAIM_REVIEW_SCHEMA
    from .semantic_evaluation_sets import SCHEMA as SEMANTIC_SET_SCHEMA
    from .research_bindings import SCHEMA as BINDING_SCHEMA
    for statement in (CLAIM_REVIEW_SCHEMA + SEMANTIC_SET_SCHEMA + BINDING_SCHEMA).split(';'):
        if statement.strip():
            connection.execute(statement)


def _migrate_v16(connection):
    from .industry_mom_storage_schema import SCHEMA as INDUSTRY_MOM_SCHEMA
    for statement in INDUSTRY_MOM_SCHEMA.split(';'):
        if statement.strip():
            connection.execute(statement)


@contextmanager
def _schema_lock(path, timeout=15):
    # WAL mode itself needs a database lock before BEGIN IMMEDIATE can protect
    # migrations. Separate open descriptors serialize both threads and processes.
    path = Path(path)
    descriptor = os.open(path.with_name(path.name + '.schema.lock'),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError('Database schema lock must be a regular file')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Database schema initialization is busy; retry startup')
                time.sleep(min(.025, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(descriptor)


def initialize(path):
    with _schema_lock(path):
        _initialize_locked(path)


def _initialize_locked(path):
    connection = connect(path)
    try:
        _version(connection)  # Refuse future/unidentified schemas before changing journal mode.
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('BEGIN IMMEDIATE')
        version = _version(connection)  # Recheck after obtaining the migration lock.
        if version == 0:
            # executescript implicitly commits: individual DDL statements keep
            # schema creation and upgrades in the same rollback-able transaction.
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("INSERT INTO settings VALUES ('schema_version','1')")
            version = 1
        connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)')
        if version == 1:
            _migrate_v1(connection)
            connection.execute("UPDATE settings SET value='2' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (2, datetime.now(timezone.utc).isoformat(), 'Freeze regression compatibility contracts; preserve legacy cases'))
            version = 2
        if version == 2:
            _migrate_v2(connection)
            connection.execute("UPDATE settings SET value='3' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (3, datetime.now(timezone.utc).isoformat(), 'Versioned dataset imports, declared review sources and append-only issue history'))
            version = 3
        if version == 3:
            _migrate_v3(connection)
            connection.execute("UPDATE settings SET value='4' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (4, datetime.now(timezone.utc).isoformat(), 'Optional result-bound structured research assessments; preserve historical verdicts'))
            version = 4
        if version == 4:
            _migrate_v4(connection)
            connection.execute("UPDATE settings SET value='5' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (5, datetime.now(timezone.utc).isoformat(), 'Atomic review and regression approval request receipts; preserve legacy calls'))
            version = 5
        if version == 5:
            _migrate_v5(connection)
            connection.execute("UPDATE settings SET value='6' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (6, datetime.now(timezone.utc).isoformat(), 'Atomic research creation receipts and frozen research reports; preserve historical receipts'))
            version = 6
        if version == 6:
            _migrate_v6(connection)
            connection.execute("UPDATE settings SET value='7' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (7, datetime.now(timezone.utc).isoformat(), 'Immutable workflow observations with explicit source, exact bindings and idempotent imports'))
            version = 7
        if version == 7:
            _migrate_v7(connection)
            connection.execute("UPDATE settings SET value='8' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (8, datetime.now(timezone.utc).isoformat(), 'Atomic revision and exact-result regression request receipts; preserve historical records'))
            version = 8
        if version == 8:
            _migrate_v8(connection)
            connection.execute("UPDATE settings SET value='9' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                (9, datetime.now(timezone.utc).isoformat(), 'Immutable research window and missing-data protocols; preserve existing experiments'))
            version = 9
        if version == 9:
            _migrate_v9(connection)
            connection.execute("UPDATE settings SET value='10' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (10, datetime.now(timezone.utc).isoformat(), 'Versioned monthly experiments, exact-result reviews and frozen reports'))
            version = 10
        if version == 10:
            _migrate_v10(connection)
            connection.execute("UPDATE settings SET value='11' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (11, datetime.now(timezone.utc).isoformat(), 'Immutable bounded author-panel imports and exact-request receipts'))
            version = 11
        if version == 11:
            _migrate_v11(connection)
            connection.execute("UPDATE settings SET value='12' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (12, datetime.now(timezone.utc).isoformat(), 'Immutable author eligibility studies, exact-result reviews and bounded revisions'))
            version = 12
        if version == 12:
            _migrate_v12(connection)
            connection.execute("UPDATE settings SET value='13' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (13, datetime.now(timezone.utc).isoformat(), 'Immutable research contexts and bounded provider-free tool sessions'))
            version = 13
        if version == 13:
            _migrate_v13(connection)
            connection.execute("UPDATE settings SET value='14' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (14, datetime.now(timezone.utc).isoformat(), 'Dataset temporal guards, bounded research execution and referenced claims; preserve historical records'))
            version = 14
        if version == 14:
            _migrate_v14(connection)
            connection.execute("UPDATE settings SET value='15' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (15, datetime.now(timezone.utc).isoformat(), 'Equivalent observation protection, bounded domain jobs and immutable semantic material annotations'))
            version = 15
        if version == 15:
            _migrate_v15(connection)
            connection.execute("UPDATE settings SET value='16' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (16, datetime.now(timezone.utc).isoformat(), 'Exact claim reviews, frozen semantic references and author research bindings'))
            version = 16
        if version == 16:
            _migrate_v16(connection)
            connection.execute("UPDATE settings SET value='17' WHERE key='schema_version'")
            connection.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                               (17, datetime.now(timezone.utc).isoformat(), 'Registered industry MOM sources and independent bounded experiment attempts'))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def transaction(path):
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
