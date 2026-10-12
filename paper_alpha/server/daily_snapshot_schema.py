"""Closed daily development snapshot declarations; pure identities and additive DDL."""
from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..daily_contract import CANONICAL_VERSION, MAX_RAW_BYTES, parse_contract, parse_source_receipt
from ..storage import digest

DATA_KIND = "daily_development_snapshot"
SNAPSHOT_VERSION = "daily-development-snapshot-v1"
DECLARATION_VERSION = "daily-snapshot-declaration-v1"
REGISTRATION_VERSION = "daily-snapshot-registration-v1"
DECLARATION_FIELDS = ("id", "title", "created_at", "idempotency_key", "request_digest", "input_digest",
                      "contract_file_sha256", "contract_digest", "receipt_file_sha256", "raw_sha256",
                      "raw_bytes", "contract_json", "receipt_json", "origin")
SCHEMA = """
CREATE TABLE daily_snapshot_registrations(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,input_digest TEXT NOT NULL,
 contract_file_sha256 TEXT NOT NULL,contract_digest TEXT NOT NULL,receipt_file_sha256 TEXT NOT NULL,
 raw_sha256 TEXT NOT NULL,raw_bytes INTEGER NOT NULL,contract_json TEXT NOT NULL,receipt_json TEXT NOT NULL,
 origin TEXT NOT NULL,declaration_digest TEXT NOT NULL,owner_token TEXT NOT NULL,latest_attempt_id TEXT,
 registered_dataset_id TEXT REFERENCES datasets(id),registration_digest TEXT,
 development_content_digest TEXT,development_index_digest TEXT,error TEXT);
CREATE TABLE daily_snapshot_attempts(
 id TEXT PRIMARY KEY,registration_id TEXT NOT NULL REFERENCES daily_snapshot_registrations(id),number INTEGER NOT NULL,
 status TEXT NOT NULL,stage TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,
 owner_token TEXT NOT NULL,code_digest TEXT NOT NULL,report TEXT,error TEXT,
 UNIQUE(registration_id,number));
CREATE INDEX daily_snapshot_attempt_history ON daily_snapshot_attempts(registration_id,number);
"""


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def declaration_payload(row):
    """Business immutable fields only. No state/owner/attempt, IO, or market numbers."""
    if not isinstance(row, Mapping):
        row = dict(row)
    value = {key: row[key] for key in DECLARATION_FIELDS}
    for key in ("id", "title", "created_at", "idempotency_key", "origin"):
        if type(value[key]) is not str or not value[key]:
            raise ValueError("Invalid declaration field: " + key)
    for key in ("request_digest", "input_digest", "contract_file_sha256", "contract_digest", "receipt_file_sha256", "raw_sha256"):
        if type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise ValueError("Invalid declaration digest: " + key)
    if type(value["raw_bytes"]) is not int or not 0 < value["raw_bytes"] <= MAX_RAW_BYTES:
        raise ValueError("Invalid declaration byte count")
    contract = parse_contract(_json(value["contract_json"]))
    receipt = parse_source_receipt(_json(value["receipt_json"]))
    value["contract_json"] = contract.to_dict()
    value["receipt_json"] = receipt.to_dict()
    c, r = value["contract_json"], value["receipt_json"]
    if (contract.contract_digest != value["contract_digest"] or c["origin"] != value["origin"]
            or r["origin"] != value["origin"] or c["source"]["source_id"] != r["source_id"]
            or c["source"]["receipt_sha256"] != value["receipt_file_sha256"]
            or r["raw_sha256"] != value["raw_sha256"] or r["raw_bytes"] != value["raw_bytes"]):
        raise ValueError("Declaration identities disagree")
    expected = digest({key: value[key] for key in ("contract_file_sha256", "receipt_file_sha256", "raw_sha256")})
    if expected != value["input_digest"]:
        raise ValueError("Declaration input digest disagrees")
    return {"declaration_version": DECLARATION_VERSION, **value}


def declaration_digest(row):
    return digest(declaration_payload(row))


def development_index_digest(view):
    return digest({"canonical_version": CANONICAL_VERSION,
                   "observations": [{"date": o.date, "asset": o.asset, "row_sha256": o.row_sha256}
                                    for o in view.observations]})


def snapshot_registration_digest(registration, dataset):
    return digest({"registration_version": REGISTRATION_VERSION,
                   "registration_id": registration["id"], "declaration_digest": registration["declaration_digest"],
                   "dataset_id": dataset["id"], "dataset_sha256": dataset["sha256"],
                   "development_content_digest": registration["development_content_digest"],
                   "development_index_digest": registration["development_index_digest"]})


class DailySnapshotAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    number: int
    status: Literal["prechecking", "registered", "failed", "interrupted"]
    stage: str
    started_at: str
    finished_at: str | None
    code_digest: str
    error: str | None


class DailySnapshotSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    id: str
    title: str
    status: Literal["reserved", "prechecking", "registered", "failed", "interrupted"]
    created_at: str
    updated_at: str
    input_digest: str
    contract_digest: str
    declaration_digest: str
    registered_dataset_id: str | None
    registration_digest: str | None
    development_content_digest: str | None
    development_index_digest: str | None
    data_kind: Literal["daily_development_snapshot"] = DATA_KIND
    origin: Literal["engineering_fixture"] = "engineering_fixture"
    scope: Literal["development_only"] = "development_only"
    full_dataset_validated: Literal[False] = False
    final_execution_allowed: Literal[False] = False
    final_numeric_rows_parsed: Literal[0] = 0
    test_freshness: Literal["unknown"] = "unknown"
    real_source_ready: Literal[False] = False
    error: str | None
    attempts: list[DailySnapshotAttempt] = Field(default_factory=list)
