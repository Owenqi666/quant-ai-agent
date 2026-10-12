"""Separate final-period permission and permanent exposure records.

Schema27 migration/backup allow only these exact protection triggers. Local
helpers expose insert/replay only, with independent frozen business
anchors, permanent unique program exposure and verified asset/date inventory.
There is no permission/exposure deletion, refund or update API.
"""
from typing import Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel

SCHEMA = """
CREATE TABLE daily_holdout_freezes(id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,program_id TEXT NOT NULL,revision_id TEXT NOT NULL REFERENCES daily_revision_bindings(revision_id),run_id TEXT NOT NULL REFERENCES runs(id),attempt_id TEXT NOT NULL REFERENCES attempts(id),registration_id TEXT NOT NULL REFERENCES daily_snapshot_registrations(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE daily_holdout_permissions(id TEXT PRIMARY KEY,freeze_id TEXT NOT NULL UNIQUE REFERENCES daily_holdout_freezes(id),authorization_kind TEXT NOT NULL CHECK(authorization_kind IN ('fixture','real')),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE daily_holdout_exposures(id TEXT PRIMARY KEY,freeze_id TEXT NOT NULL UNIQUE REFERENCES daily_holdout_freezes(id),program_id TEXT NOT NULL UNIQUE,permission_id TEXT NOT NULL REFERENCES daily_holdout_permissions(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE daily_holdout_exposure_keys(exposure_id TEXT NOT NULL REFERENCES daily_holdout_exposures(id),date TEXT NOT NULL,asset TEXT NOT NULL,PRIMARY KEY(exposure_id,date,asset));
CREATE INDEX daily_holdout_key_lookup ON daily_holdout_exposure_keys(date,asset,exposure_id);
CREATE TABLE daily_holdout_events(exposure_id TEXT NOT NULL REFERENCES daily_holdout_exposures(id),sequence INTEGER NOT NULL CHECK(sequence>0),payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(exposure_id,sequence));
CREATE TRIGGER holdout_freezes_no_update BEFORE UPDATE ON daily_holdout_freezes BEGIN SELECT RAISE(ABORT,'immutable holdout freeze'); END;
CREATE TRIGGER holdout_freezes_no_delete BEFORE DELETE ON daily_holdout_freezes BEGIN SELECT RAISE(ABORT,'permanent holdout freeze'); END;
CREATE TRIGGER holdout_permissions_no_update BEFORE UPDATE ON daily_holdout_permissions BEGIN SELECT RAISE(ABORT,'immutable final permission'); END;
CREATE TRIGGER holdout_permissions_no_delete BEFORE DELETE ON daily_holdout_permissions BEGIN SELECT RAISE(ABORT,'permanent final permission'); END;
CREATE TRIGGER holdout_exposures_no_update BEFORE UPDATE ON daily_holdout_exposures BEGIN SELECT RAISE(ABORT,'permanent final exposure'); END;
CREATE TRIGGER holdout_exposures_no_delete BEFORE DELETE ON daily_holdout_exposures BEGIN SELECT RAISE(ABORT,'permanent final exposure'); END;
CREATE TRIGGER holdout_keys_no_update BEFORE UPDATE ON daily_holdout_exposure_keys BEGIN SELECT RAISE(ABORT,'immutable exposed key'); END;
CREATE TRIGGER holdout_keys_no_delete BEFORE DELETE ON daily_holdout_exposure_keys BEGIN SELECT RAISE(ABORT,'permanent exposed key'); END;
CREATE TRIGGER holdout_events_no_update BEFORE UPDATE ON daily_holdout_events BEGIN SELECT RAISE(ABORT,'immutable final observation'); END;
CREATE TRIGGER holdout_events_no_delete BEFORE DELETE ON daily_holdout_events BEGIN SELECT RAISE(ABORT,'permanent final observation'); END;
"""
REQUIRED_COLUMNS = {
    'daily_holdout_freezes': {'id','idempotency_key','program_id','revision_id','run_id','attempt_id','registration_id','payload','digest'},
    'daily_holdout_permissions': {'id','freeze_id','authorization_kind','payload','digest'},
    'daily_holdout_exposures': {'id','freeze_id','program_id','permission_id','payload','digest'},
    'daily_holdout_exposure_keys': {'exposure_id','date','asset'},
    'daily_holdout_events': {'exposure_id','sequence','payload','digest'},
}

class DailyHoldoutFreezeCreate(AuthorModel):
    program_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    revision_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    expected_result_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    candidate_ids: list[str] = Field(min_length=1, max_length=2)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')

    @model_validator(mode='after')
    def candidates(self):
        if len(set(self.candidate_ids)) != len(self.candidate_ids) or any(not c or len(c)>128 for c in self.candidate_ids):
            raise ValueError('Freeze one or two distinct bounded candidate identities')
        return self

class DailyHoldoutPermissionCreate(AuthorModel):
    freeze_id: str = Field(min_length=1, max_length=128)
    authorization_kind: Literal['fixture','real'] = 'fixture'
    human_authorization_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    max_seconds: int = Field(default=240, ge=1, le=7200)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')

    @model_validator(mode='after')
    def explicit_real(self):
        if self.authorization_kind == 'real' and self.human_authorization_digest is None:
            raise ValueError('Real final reads require their scoped explicit human receipt')
        return self
