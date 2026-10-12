"""Main Store authority for metadata-only, bounded OHLC development admission.

No raw originals, prefixes, providers or Workers are opened or invoked here.
Saved scopes survive expiry, revocation, restore and implementation changes.
Current permission is a separate projection under the present workspace epoch.
Only explicit trusted maintenance callers bootstrap or rotate identity files.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from ..storage import digest, json_text
from .db import connect, transaction
from .maintenance import workspace_lease
from .real_daily_workspace_authority_schema import AUTHORITY_VERSION, REQUIRED_COLUMNS

ANCHOR_FILE = '.real-daily-authority.json'
EXECUTION_FILE = '.real-daily-execution.json'
SETTING_VERSION = 'real_daily_workspace_authority_version'
SETTING_ID = 'real_daily_workspace_authority_id'
SETTING_EPOCH = 'real_daily_workspace_current_epoch_id'
TABLES = frozenset(REQUIRED_COLUMNS)
MAX_HISTORY = 1000
MAX_SCOPE_KEYS = 100_000
MAX_HISTORY_METADATA = 16 * 1024 * 1024
MAX_METADATA = 2 * 1024 * 1024
MAX_IDENTITY_BYTES = 65536
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_ASSESSMENT_KEYS = frozenset({'schema_version','assessment_version','selection','ledger','cash_annotations','source_facts','input_pins','method','method_digest','event_review','code_identity','blockers','status','origin','source_authenticated','real_source_ready','point_in_time','local_development_execution_allowed','final_numeric_access_allowed','final_numeric_rows_parsed','limitations','assessment_digest'})


class RealDailyWorkspaceAuthorityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(ok, code):
    if not ok:
        raise RealDailyWorkspaceAuthorityError(code)


def _closed(value, keys, code='authority_history_invalid'):
    _need(type(value) is dict and set(value) == set(keys), code)


def _pin(value):
    _need(type(value) is str and _SHA.fullmatch(value) is not None, 'invalid_pin')
    return value


def _text(value, maximum=128):
    _need(type(value) is str and 0 < len(value) <= maximum and value.strip() == value and not any(ord(c)<32 for c in value), 'invalid_text')
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _utc(value):
    _text(value, 48)
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        _need(stamp.tzinfo is not None and stamp.utcoffset().total_seconds()==0, 'invalid_utc')
        return stamp
    except (ValueError, TypeError):
        raise RealDailyWorkspaceAuthorityError('invalid_utc') from None


def _snapshot(value):
    # Exact builtins prevent arbitrary mapping/copy/serialization hooks.
    count=0
    def clone(item, depth=0):
        nonlocal count
        count+=1
        _need(count<=100000 and depth<=64, 'bounded_metadata_required')
        if type(item) is dict:
            _need(len(item)<=100000, 'bounded_metadata_required')
            out={}
            for key,val in tuple(item.items()):
                _need(type(key) is str and len(key)<=256, 'invalid_metadata')
                out[key]=clone(val,depth+1)
            return out
        if type(item) is list:
            _need(len(item)<=100000, 'bounded_metadata_required')
            return [clone(v,depth+1) for v in tuple(item)]
        _need(type(item) in (str,int,float,bool) or item is None, 'invalid_metadata')
        if type(item) is str: _need(len(item)<=65536, 'bounded_metadata_required')
        if type(item) is int: _need(abs(item)<=2**63-1, 'bounded_metadata_required')
        return item
    try:
        out=clone(value)
        _need(len(json_text(out).encode())<=MAX_METADATA, 'bounded_metadata_required')
        return out
    except RealDailyWorkspaceAuthorityError:
        raise
    except (ValueError,TypeError,UnicodeError,RecursionError,OverflowError,RuntimeError):
        raise RealDailyWorkspaceAuthorityError('invalid_metadata') from None


def _json(raw):
    try:
        _need(type(raw) in (str,bytes) and len(raw)<=MAX_METADATA, 'bounded_metadata_required')
        def pairs(values):
            out={}
            for k,v in values:
                _need(k not in out, 'invalid_metadata');out[k]=v
            return out
        return _snapshot(json.loads(raw,object_pairs_hook=pairs))
    except RealDailyWorkspaceAuthorityError:
        raise
    except (ValueError,TypeError,UnicodeError,RecursionError,OverflowError):
        raise RealDailyWorkspaceAuthorityError('invalid_metadata') from None


def _rows(connection, sql, values=()):
    cursor=connection.cursor()
    cursor.row_factory=sqlite3.Row
    try:
        return [dict(row) for row in cursor.execute(sql,values).fetchall()]
    finally:
        cursor.close()


def _setting(connection,key):
    rows=_rows(connection,'SELECT value FROM settings WHERE key=?',(key,))
    return rows[0]['value'] if rows else None


def _root(connection):
    rows=_rows(connection,'PRAGMA database_list')
    paths=[r['file'] for r in rows if r['name']=='main']
    _need(len(paths)==1 and paths[0] and Path(paths[0]).is_absolute(), 'authority_database_path_required')
    return Path(paths[0]).parent


def _root_literal(value):
    _need(type(value) is str and value.startswith('/') and len(value)<=4096 and value != '/' and all(p and p not in ('.','..') for p in value[1:].split('/')), 'unsafe_authority_path')
    return value


def _safe_root(root):
    root=Path(root)
    _need(root.is_absolute() and '..' not in root.parts and root.resolve()==root and not any(p.is_symlink() for p in (root,*root.parents)), 'unsafe_authority_path')
    return root


def _stamp(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink,info.st_mode)


def _read_identity(root,name):
    _need(name in (ANCHOR_FILE,EXECUTION_FILE),'invalid_identity_file')
    root=_safe_root(root);directory=None;stream=None
    try:
        directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        for component in root.parts[1:]:
            next_fd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=next_fd
        before=os.stat(name,dir_fd=directory,follow_symlinks=False)
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=MAX_IDENTITY_BYTES,'authority_identity_invalid')
        stream=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        _need(_stamp(os.fstat(stream))==_stamp(before),'authority_identity_drift')
        raw=os.read(stream,MAX_IDENTITY_BYTES+1)
        _need(len(raw)==before.st_size and _stamp(os.fstat(stream))==_stamp(before)==_stamp(os.stat(name,dir_fd=directory,follow_symlinks=False)),'authority_identity_drift')
        return _json(raw),hashlib.sha256(raw).hexdigest()
    except OSError:
        raise RealDailyWorkspaceAuthorityError('authority_identity_missing') from None
    finally:
        if stream is not None:os.close(stream)
        if directory is not None:os.close(directory)


def _write_identity(root,name,value,*,replace=False):
    root=_safe_root(root);path=root/name;raw=json_text(value).encode()
    _need(len(raw)<=MAX_IDENTITY_BYTES,'bounded_metadata_required')
    # Maintenance owns the root while these fixed metadata paths are written.
    target=path if not replace else root/('.rdw-identity-'+uuid.uuid4().hex+'.tmp')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        offset=0
        while offset<len(raw):offset+=os.write(fd,raw[offset:])
        os.fsync(fd)
    finally:os.close(fd)
    try:
        if replace:
            _read_identity(root,name)
            os.replace(target,path)
        directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
    except BaseException:
        if replace and target.exists():target.unlink()
        raise
    return hashlib.sha256(raw).hexdigest()


def _state(connection):
    names={r['name'] for r in _rows(connection,"SELECT name FROM sqlite_master WHERE type='table'")}
    present=names&TABLES
    version=_setting(connection,'schema_version') if 'settings' in names else None
    settings={k:_setting(connection,k) for k in (SETTING_VERSION,SETTING_ID,SETTING_EPOCH)} if 'settings' in names else {}
    root=None
    try:root=_root(connection)
    except RealDailyWorkspaceAuthorityError:pass
    fragments=bool(any(value is not None for value in settings.values()) or root and any(os.path.lexists(root/n) for n in (ANCHOR_FILE,EXECUTION_FILE)))
    if not present and not fragments and version not in ('28','29'):return None
    _need(present==TABLES,'authority_schema_missing')
    for table,expected in REQUIRED_COLUMNS.items():
        _need({r['name'] for r in _rows(connection,'PRAGMA table_info('+table+')')}==expected,'authority_schema_invalid')
    _need(version in ('28','29'),'authority_schema_invalid')
    from .db import verify_audit_triggers
    try:verify_audit_triggers(connection)
    except RuntimeError:raise RealDailyWorkspaceAuthorityError('authority_trigger_drift') from None
    has_records=any(_rows(connection,'SELECT 1 AS present FROM '+table+' LIMIT 1') for table in TABLES)
    if not fragments and not has_records:return None
    _need(root is not None and settings.get(SETTING_VERSION)==AUTHORITY_VERSION and all(settings.values()),'authority_identity_missing')
    anchor,anchor_sha=_read_identity(root,ANCHOR_FILE)
    _closed(anchor,{'schema_version','authority_version','authority_id','created_at_utc','scope','final_numeric_access_allowed'})
    _need(type(anchor['schema_version']) is int and anchor['schema_version']==1 and anchor['authority_version']==AUTHORITY_VERSION and anchor['authority_id']==settings[SETTING_ID] and anchor['scope']=='local_development_only' and anchor['final_numeric_access_allowed'] is False,'authority_identity_drift')
    _text(anchor['authority_id']);_utc(anchor['created_at_utc'])
    return {'root':root,'anchor':anchor,'anchor_sha256':anchor_sha,'settings':settings}


def _record(row,digest_field):
    _need(row is not None,'authority_record_missing')
    value=_json(row['payload_json']);_need(digest(value)==_pin(row[digest_field]),'authority_record_drift')
    return value


def _saved_assessment(row):
    value=_json(row['payload_json']);_closed(value,_ASSESSMENT_KEYS)
    _need(value['assessment_digest']==row['assessment_digest']==digest({k:v for k,v in value.items() if k!='assessment_digest'}),'assessment_anchor_drift')
    from ..real_daily_admission import VERSION,FACTS
    from ..real_daily_web_contract import validate_selection
    _need(type(value['schema_version']) is int and value['schema_version']==1 and value['assessment_version']==VERSION,'assessment_anchor_drift')
    selection=validate_selection(value['selection'])
    _need(json_text(selection)==json_text(value['selection']),'assessment_anchor_drift')
    _need(value['origin'] in ('engineering_fixture','real_source_declaration') and value['origin']==value['input_pins']['origin'],'assessment_anchor_drift')
    _need(all(value[k] is False for k in ('source_authenticated','real_source_ready','local_development_execution_allowed','final_numeric_access_allowed')) and type(value['final_numeric_rows_parsed']) is int and value['final_numeric_rows_parsed']==0 and value['point_in_time']=='not_certified','assessment_scope_drift')
    _need(value['method_digest']==digest(value['method']) and value['method']['required_fields']==['open','high','low','close'] and value['method']['final_numeric_read_allowed'] is False and value['method']['total_return'] is False,'assessment_scope_drift')
    _closed(value['source_facts'],FACTS)
    source_blockers=[]
    for name in FACTS:
        fact=value['source_facts'][name];_closed(fact,{'status','statement','evidence_sha256'})
        _need(fact['status'] in ('supported','unknown','rejected'),'assessment_scope_drift')
        if fact['status']!='supported':source_blockers.append({'code':'source_fact_'+fact['status'],'fact':name})
    events=value['event_review']
    _need(type(events) is dict and type(events.get('blockers')) is list and events.get('method_digest')==value['method_digest'] and events.get('selection_digest')==digest(selection),'assessment_anchor_drift')
    _need(json_text(value['blockers'])==json_text(source_blockers+events['blockers']) and value['status']==('blocked' if value['blockers'] else 'ready_for_host_review'),'assessment_scope_drift')
    code=value['code_identity'];_closed(code,{'version','files','digest'})
    _need(code['version']==VERSION and type(code['files']) is dict and 0<len(code['files'])<=50 and code['digest']==digest(code['files']),'assessment_anchor_drift')
    for name,pin in code['files'].items():
        _need(type(name) is str and name.startswith('paper_alpha/') and '..' not in name.split('/'),'assessment_anchor_drift');_pin(pin)
    return value


def _keys(selection):
    dev=set(selection['development_sessions'])
    return [(day,asset,'development' if day in dev else 'test') for day in selection['sessions'] for asset in selection['assets']]


def _history(connection):
    state=_state(connection)
    if state is None:return [],None
    aid=state['anchor']['authority_id'];tables={}
    total_metadata=0
    for table in TABLES-{'rdw_scope_keys'}:
        # Refuse an oversized record population before any payload aggregation;
        # measure UTF-8 bytes, rather than SQLite text character counts.
        population=_rows(connection,'SELECT 1 AS present FROM '+table+' LIMIT ?',(MAX_HISTORY+1,))
        _need(len(population)<=MAX_HISTORY, 'history_bound_exceeded')
        total_metadata+=_rows(connection, 'SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) AS size FROM '+table)[0]['size']
    _need(total_metadata<=MAX_HISTORY_METADATA, 'history_bound_exceeded')
    for table in TABLES:
        rows=_rows(connection,'SELECT * FROM '+table+' LIMIT ?',(MAX_SCOPE_KEYS+1 if table=='rdw_scope_keys' else MAX_HISTORY+1,))
        _need(len(rows)<= (MAX_SCOPE_KEYS if table=='rdw_scope_keys' else MAX_HISTORY),'history_bound_exceeded')
        tables[table]=rows
    epochs={}
    for row in tables['rdw_authority_epochs']:
        value=_record(row,'epoch_digest');_closed(value,{'schema_version','epoch_version','id','authority_id','authority_anchor_sha256','source_epoch_id','kind','execution_root','created_at_utc'})
        _need(type(value['schema_version']) is int and value['schema_version']==1 and value['epoch_version']==AUTHORITY_VERSION and value['id']==row['id'] and value['authority_id']==row['authority_id']==aid and value['authority_anchor_sha256']==state['anchor_sha256'] and value['source_epoch_id']==row['source_epoch_id'] and value['kind']==row['kind'] and value['created_at_utc']==row['created_at'],'authority_epoch_drift')
        _need(value['kind'] in ('initial','restore') and ((value['kind']=='initial')==(value['source_epoch_id'] is None)),'authority_epoch_drift')
        _utc(value['created_at_utc']);_root_literal(value['execution_root']);epochs[row['id']]=value
    _need(len([e for e in epochs.values() if e['kind']=='initial'])==1 and state['settings'][SETTING_EPOCH] in epochs,'authority_epoch_missing')
    for identity,value in epochs.items():
        seen={identity};parent=value['source_epoch_id']
        while parent is not None:
            _need(parent in epochs and parent not in seen,'authority_epoch_drift');seen.add(parent);parent=epochs[parent]['source_epoch_id']
    parents={e['source_epoch_id'] for e in epochs.values() if e['source_epoch_id'] is not None}
    terminal=set(epochs)-parents
    _need(terminal=={state['settings'][SETTING_EPOCH]}, 'authority_epoch_drift')
    assessments={}
    for row in tables['rdw_assessments']:
        _need(row['authority_id']==aid,'assessment_anchor_drift');_pin(row['request_digest']);_text(row['idempotency_key']);_utc(row['created_at'])
        assessments[row['id']]=_saved_assessment(row)
    approvals={}
    for row in tables['rdw_approvals']:
        value=_record(row,'approval_digest');_closed(value,{'schema_version','approval_version','id','authority_id','epoch_id','execution_root','assessment_id','assessment_digest','method_digest','selection_digest','reviewer','created_at_utc','expires_at_utc','scope','numeric_access','whole_byte_integrity_allowed','final_numeric_access_allowed','source_authenticated','real_source_ready','point_in_time'})
        _need(type(value['schema_version']) is int and value['schema_version']==1 and value['approval_version']==AUTHORITY_VERSION and value['id']==row['id'] and value['authority_id']==row['authority_id']==aid and value['epoch_id']==row['epoch_id'] and value['epoch_id'] in epochs and value['execution_root']==epochs[value['epoch_id']]['execution_root'] and value['assessment_id']==row['assessment_id'] and value['assessment_id'] in assessments and value['created_at_utc']==row['created_at'],'approval_binding_drift')
        assessment=assessments[value['assessment_id']]
        _need(value['assessment_digest']==assessment['assessment_digest'] and value['method_digest']==assessment['method_digest'] and value['selection_digest']==digest(assessment['selection']) and not assessment['blockers'] and assessment['status']=='ready_for_host_review','approval_scope_drift')
        _need(value['scope']=='local_development_only' and value['numeric_access']=='development_ohlc_only' and value['whole_byte_integrity_allowed'] is True and all(value[k] is False for k in ('final_numeric_access_allowed','source_authenticated','real_source_ready')) and value['point_in_time']=='not_certified' and _utc(value['created_at_utc'])<_utc(value['expires_at_utc']),'approval_scope_drift')
        _text(value['reviewer'],200);_pin(row['request_digest']);_text(row['idempotency_key']);approvals[row['id']]=(row,value)
    revoked={}
    for row in tables['rdw_revocations']:
        value=_record(row,'revocation_digest');_closed(value,{'schema_version','approval_id','approval_digest','reason','revoked_at_utc'})
        _need(type(value['schema_version']) is int and value['schema_version']==1 and value['approval_id']==row['approval_id'] and value['approval_id'] in approvals and value['approval_digest']==approvals[value['approval_id']][0]['approval_digest'] and value['revoked_at_utc']==row['created_at'],'revocation_binding_drift')
        _text(value['reason'],1000);_utc(value['revoked_at_utc']);_pin(row['request_digest']);_text(row['idempotency_key']);revoked[row['approval_id']]=value
    scopes=[];owners=set();key_index={}
    for key in tables['rdw_scope_keys']:
        key_index.setdefault(key['reservation_id'], []).append((key['date'],key['asset'],key['role']))
    for row in tables['rdw_reservations']:
        value=_record(row,'reservation_digest');_closed(value,{'schema_version','reservation_version','id','authority_id','epoch_id','execution_root','approval_id','approval_digest','assessment_id','assessment_digest','selection_digest','scope_keys_digest','created_at_utc','final_numeric_access_allowed'})
        _need(type(value['schema_version']) is int and value['schema_version']==1 and value['reservation_version']==AUTHORITY_VERSION and value['id']==row['id'] and value['authority_id']==row['authority_id']==aid and value['epoch_id']==row['epoch_id'] and value['approval_id']==row['approval_id'] and value['approval_id'] in approvals and value['created_at_utc']==row['created_at'] and value['final_numeric_access_allowed'] is False,'reservation_binding_drift')
        ap_row,approval=approvals[value['approval_id']];assessment=assessments[approval['assessment_id']]
        _need(value['approval_digest']==ap_row['approval_digest'] and value['assessment_id']==approval['assessment_id'] and value['assessment_digest']==approval['assessment_digest'] and value['selection_digest']==approval['selection_digest'] and value['epoch_id']==approval['epoch_id'] and value['execution_root']==approval['execution_root'],'reservation_binding_drift')
        expected=_keys(assessment['selection']);actual=sorted(key_index.get(row['id'],[]))
        _need(actual==expected and value['scope_keys_digest']==digest(expected),'reservation_keys_drift')
        _pin(row['request_digest']);_text(row['idempotency_key']);owners.add(row['id'])
        scopes.append({'id':row['id'],'reservation_digest':row['reservation_digest'],'approval_id':value['approval_id'],'selection':deepcopy(assessment['selection'])})
    _need(all(k['reservation_id'] in owners for k in tables['rdw_scope_keys']),'orphan_scope_keys')
    projection,_=_read_identity(state['root'],EXECUTION_FILE)
    _need(json_text(projection)==json_text(_projection(state,epochs[state['settings'][SETTING_EPOCH]])), 'authority_identity_drift')
    state.update(epochs=epochs,assessments=assessments,approvals=approvals,revocations=revoked)
    return scopes,state


def assert_history(connection):
    """Read frozen declared metadata under actual DB-parent anchors, including backups."""
    try:return _history(connection)[0]
    except RealDailyWorkspaceAuthorityError:raise
    except (sqlite3.Error,ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        raise RealDailyWorkspaceAuthorityError('authority_history_invalid') from None


def verify_workspace_metadata(connection):
    """Verify both managed identity witnesses at actual DB parent, not live grants."""
    _,state=_history(connection)
    if state is None:return {"initialized":False,"current_permission":"not_checked"}
    return {"initialized":True,"authority_id":state["anchor"]["authority_id"],"current_epoch_id":state["settings"][SETTING_EPOCH],"metadata_integrity_verified":True,"current_permission":"not_checked","current_root_equality":"not_checked"}


def check_main_real_overlap(connection, *, assets, development_sessions, test_sessions):
    scopes=assert_history(connection);assets=set(assets);dev=set(development_sessions);test=set(test_sessions)
    for scope in scopes:
        selection=scope['selection']
        if assets&set(selection['assets']) and (dev&set(selection['partitions']['test']) or test&set(selection['development_sessions'])):
            return 'Main real daily potential asset/date overlap with retained reservation '+scope['id']
    return None


def _projection(state,epoch):
    return {'schema_version':1,'authority_version':AUTHORITY_VERSION,'authority_id':state['anchor']['authority_id'],'current_epoch_id':epoch['id'],'epoch_digest':digest(epoch),'execution_root':epoch['execution_root'],'restored_dispatch_paused':epoch['kind']=='restore'}


def _epoch(anchor,anchor_sha,root,*,parent=None):
    return {'schema_version':1,'epoch_version':AUTHORITY_VERSION,'id':uuid.uuid4().hex,'authority_id':anchor['authority_id'],'authority_anchor_sha256':anchor_sha,'source_epoch_id':parent,'kind':'restore' if parent else 'initial','execution_root':str(_safe_root(root)),'created_at_utc':_now()}


def bootstrap_authority_metadata(existing_store):
    """Root caller holds exclusive maintenance lease; no nested lease or migration.

    Only a completely dormant schema28 can activate. Partial witnesses are
    refused, never repaired. Unexpected process loss remains fail-closed.
    """
    root=_safe_root(existing_store.root);created=[]
    _need(Path(existing_store.db_path)==root/'workbench.sqlite3', 'workspace_identity_drift')
    try:
        with transaction(existing_store.db_path) as connection:
            scopes,state=_history(connection)
            if state is not None:
                execution,_=_read_identity(root,EXECUTION_FILE)
                expected=_projection(state,state['epochs'][state['settings'][SETTING_EPOCH]])
                _need(json_text(execution)==json_text(expected) and execution['execution_root']==str(root),'workspace_identity_drift')
                return {'authority_id':state['anchor']['authority_id'],'current_epoch_id':execution['current_epoch_id'],'initialized':True}
            # _state already distinguished an intact dormant schema from fragments.
            _need(_setting(connection,'schema_version') in ('28','29') and not any(_rows(connection,'SELECT 1 AS p FROM '+t+' LIMIT 1') for t in TABLES),'authority_not_initialized')
            anchor={'schema_version':1,'authority_version':AUTHORITY_VERSION,'authority_id':uuid.uuid4().hex,'created_at_utc':_now(),'scope':'local_development_only','final_numeric_access_allowed':False}
            anchor_sha=_write_identity(root,ANCHOR_FILE,anchor);created.append((ANCHOR_FILE,anchor_sha,_stamp((root/ANCHOR_FILE).stat(follow_symlinks=False))))
            epoch=_epoch(anchor,anchor_sha,root)
            connection.execute('INSERT INTO rdw_authority_epochs VALUES (?,?,?,?,?,?,?)',(epoch['id'],epoch['authority_id'],None,'initial',json_text(epoch),digest(epoch),epoch['created_at_utc']))
            for key,value in ((SETTING_VERSION,AUTHORITY_VERSION),(SETTING_ID,anchor['authority_id']),(SETTING_EPOCH,epoch['id'])):connection.execute('INSERT INTO settings VALUES (?,?)',(key,value))
            projection=_projection({'anchor':anchor},epoch);projection_sha=_write_identity(root,EXECUTION_FILE,projection);created.append((EXECUTION_FILE,projection_sha,_stamp((root/EXECUTION_FILE).stat(follow_symlinks=False))))
            _history(connection)
        return {'authority_id':anchor['authority_id'],'current_epoch_id':epoch['id'],'initialized':True}
    except BaseException:
        # Normal transactional failure may remove only files this invocation
        # created and whose bytes still match; no repair of previous witnesses.
        for name,expected,expected_stamp in reversed(created):
            try:
                _,actual=_read_identity(root,name)
                if actual==expected and _stamp((root/name).stat(follow_symlinks=False))==expected_stamp:(root/name).unlink()
            except (OSError,RealDailyWorkspaceAuthorityError):pass
        raise


def rotate_restored_epoch(connection,destination_root):
    """Within Root's restore transaction/exclusive marker; never rewrites grants."""
    _,state=_history(connection)
    if state is None:return {'initialized':False}
    root=_safe_root(destination_root);_need(_root(connection)==root,'workspace_identity_drift')
    previous=state['settings'][SETTING_EPOCH]
    old_projection,_=_read_identity(root,EXECUTION_FILE)
    _need(json_text(old_projection)==json_text(_projection(state,state['epochs'][previous])), 'authority_identity_drift')
    epoch=_epoch(state['anchor'],state['anchor_sha256'],root,parent=previous)
    # Refuse a capacity increase before replacing the filesystem witness. The
    # caller's transaction may roll back SQLite but cannot roll back that file.
    _need(len(state['epochs'])+1<=MAX_HISTORY, 'history_bound_exceeded')
    persisted_bytes=sum(_rows(connection, 'SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) AS size FROM '+table)[0]['size'] for table in TABLES-{'rdw_scope_keys'})
    _need(persisted_bytes+len(json_text(epoch).encode())<=MAX_HISTORY_METADATA, 'history_bound_exceeded')
    connection.execute('INSERT INTO rdw_authority_epochs VALUES (?,?,?,?,?,?,?)',(epoch['id'],epoch['authority_id'],previous,'restore',json_text(epoch),digest(epoch),epoch['created_at_utc']))
    connection.execute('UPDATE settings SET value=? WHERE key=?',(epoch['id'],SETTING_EPOCH))
    projection=_projection(state,epoch);_write_identity(root,EXECUTION_FILE,projection,replace=True)
    from .provider_recovery_schema import pause_dispatch
    pause_dispatch(connection,'restored_workspace')
    _history(connection)
    return {'initialized':True,'authority_id':epoch['authority_id'],'current_epoch_id':epoch['id'],'current_permission':'restored_inactive','dispatch_paused':True}


def _authority_code_identity():
    from ..real_daily_admission import code_identity,_read
    value=code_identity();root=Path(__file__).resolve().parents[2]
    for name in ('paper_alpha/server/real_daily_workspace_authority_schema.py',
            'paper_alpha/server/real_daily_workspace_authority.py', 'paper_alpha/real_daily_web_contract.py',
            'paper_alpha/server/maintenance.py', 'paper_alpha/server/real_daily_prefix_schema.py',
            'paper_alpha/real_daily_main_metadata.py', 'paper_alpha/server/real_daily_source_handles.py',
            'paper_alpha/server/real_daily_prefix_registration.py', 'paper_alpha/real_daily_main_task.py',
            'paper_alpha/server/real_daily_main_research.py', 'paper_alpha/real_daily_main_workflow.py',
            'paper_alpha/server/real_daily_main_execution.py', 'paper_alpha/server/service.py',
            'paper_alpha/server/research_guard.py', 'paper_alpha/server/execution_lifecycle.py',
            'paper_alpha/server/runner.py'):
        value['files'][name]=hashlib.sha256(_read(root/name)).hexdigest()
    value['digest']=digest(value['files'])
    return value


def _current_inputs(assessment):
    from ..real_daily_admission import _read, _path
    _need(_authority_code_identity()==assessment['code_identity'],'producer_code_drift')
    # Metadata/code descriptors alone are read. Raw descriptors and final rows
    # stay opaque; whole-raw byte verification belongs to the later B3 gate.
    for descriptor in assessment['input_pins']['metadata']+assessment['input_pins']['code']:
        try:
            _need(hashlib.sha256(_read(_path(descriptor['path']))).hexdigest()==descriptor['sha256'],'input_pin_drift')
        except OSError:
            raise RealDailyWorkspaceAuthorityError('input_unavailable') from None


class RealDailyWorkspaceAuthority:
    def __init__(self,existing_store):
        self.store=existing_store
        self.root=Path(existing_store.root)
        self.db_path=Path(existing_store.db_path)
        _need(self.db_path==self.root/'workbench.sqlite3', 'workspace_identity_drift')

    @contextmanager
    def _transaction(self):
        with workspace_lease(self.root):
            with transaction(self.db_path) as connection:
                scopes,state=_history(connection);_need(state is not None,'authority_not_initialized')
                yield connection,state
                # Commit only a state that subsequent history reads accept.
                # All append operations, including revocation, share this gate.
                _history(connection)

    def _saved(self,connection,table,identity):
        _text(identity)
        rows=_rows(connection,'SELECT * FROM '+table+' WHERE '+('approval_id' if table=='rdw_revocations' else 'id')+'=?',(identity,))
        _need(len(rows)==1,'authority_record_missing')
        return rows[0]

    @staticmethod
    def _assessment_summary(row,value):
        return {'id':row['id'],'assessment_id':row['id'],'assessment_digest':row['assessment_digest'],'status':value['status'],'blockers':deepcopy(value['blockers']),'origin':value['origin'],'method_digest':value['method_digest'],'selection_digest':digest(value['selection']),'source_authenticated':False,'real_source_ready':False,'point_in_time':'not_certified','local_development_execution_allowed':False,'final_numeric_access_allowed':False,'final_numeric_rows_parsed':0}

    def _current(self,connection,state,row):
        approval=_record(row,'approval_digest');epoch=state['epochs'][state['settings'][SETTING_EPOCH]]
        projection,_=_read_identity(self.root,EXECUTION_FILE)
        _need(json_text(projection)==json_text(_projection(state,epoch)) and projection['execution_root']==str(self.root),'workspace_identity_drift')
        permission='active'
        if approval['epoch_id']!=epoch['id'] or approval['execution_root']!=str(self.root):permission='restored_inactive'
        elif approval['id'] in state['revocations']:permission='revoked'
        elif _utc(approval['expires_at_utc'])<=_utc(_now()):permission='expired'
        else:
            try:_current_inputs(state['assessments'][approval['assessment_id']])
            except (OSError,ValueError):permission='code_or_input_changed'
        return {'id':approval['id'],'approval_id':approval['id'],'approval_digest':row['approval_digest'],'assessment_id':approval['assessment_id'],'current_permission':permission,'local_development_execution_allowed':permission=='active','source_authenticated':False,'real_source_ready':False,'point_in_time':'not_certified','scope':'local_development_only','numeric_access':'development_ohlc_only','whole_byte_integrity_allowed':True,'final_numeric_access_allowed':False}

    def submit(self,request,*,idempotency_key):
        _text(idempotency_key)
        from ..real_daily_web_contract import parse_assessment_request,assessment_from_request
        parsed=parse_assessment_request(_snapshot(request));request_pin=digest({'operation':'submit','request':parsed})
        with self._transaction() as (connection,state):
            found=_rows(connection,'SELECT * FROM rdw_assessments WHERE idempotency_key=?',(idempotency_key,))
            if found:
                _need(found[0]['request_digest']==request_pin,'idempotency_conflict');return self._assessment_summary(found[0],_saved_assessment(found[0]))
            assessment=assessment_from_request(parsed);assessment['code_identity']=_authority_code_identity()
            assessment['assessment_digest']=digest({k:v for k,v in assessment.items() if k!='assessment_digest'})
            identity=uuid.uuid4().hex;now=_now()
            connection.execute('INSERT INTO rdw_assessments VALUES (?,?,?,?,?,?,?)',(identity,state['anchor']['authority_id'],idempotency_key,request_pin,assessment['assessment_digest'],json_text(assessment),now))
            return self._assessment_summary(self._saved(connection,'rdw_assessments',identity),assessment)

    def submit_source_handle(self,handle_id,*,expected_digest,idempotency_key):
        """Only a persisted host handle may use the lexical main builder.

        Neither caller-supplied assessment state nor a builder callback is
        accepted. Resolve and append in the original authority transaction.
        """
        _text(handle_id);_pin(expected_digest);_text(idempotency_key)
        from .real_daily_source_handles import resolve_handle, assert_current_handle_metadata
        from ..real_daily_main_metadata import build_main_assessment
        with self._transaction() as (connection,state):
            handle=resolve_handle(connection,self.root,handle_id,expected_digest=expected_digest)
            request=handle['request']
            request_pin=digest({'operation':'submit_source_handle','handle_id':handle_id,
                'handle_digest':expected_digest,'request':request})
            found=_rows(connection,'SELECT * FROM rdw_assessments WHERE idempotency_key=?',(idempotency_key,))
            if found:
                _need(found[0]['request_digest']==request_pin,'idempotency_conflict')
                return self._assessment_summary(found[0],_saved_assessment(found[0]))
            assert_current_handle_metadata(self.root,handle)
            assessment=build_main_assessment(request,code_identity=_authority_code_identity())
            identity=uuid.uuid4().hex;stamp=_now()
            connection.execute('INSERT INTO rdw_assessments VALUES (?,?,?,?,?,?,?)',
                (identity,state['anchor']['authority_id'],idempotency_key,request_pin,
                 assessment['assessment_digest'],json_text(assessment),stamp))
            return self._assessment_summary(self._saved(connection,'rdw_assessments',identity),assessment)

    def get_assessment(self,assessment_id):
        with self._transaction() as (connection,state):
            row=self._saved(connection,'rdw_assessments',assessment_id);return self._assessment_summary(row,_saved_assessment(row))

    def approve(self,assessment_id,*,expected_digest,reviewer,expires_at_utc,whole_byte_integrity_allowed,numeric_access,final_numeric_access_allowed,idempotency_key):
        _pin(expected_digest);_text(reviewer,200);_text(idempotency_key);_utc(expires_at_utc)
        _need(whole_byte_integrity_allowed is True and numeric_access=='development_ohlc_only' and final_numeric_access_allowed is False,'approval_scope_required')
        command={'operation':'approve','assessment_id':assessment_id,'expected_digest':expected_digest,'reviewer':reviewer,'expires_at_utc':expires_at_utc,'whole_byte_integrity_allowed':whole_byte_integrity_allowed,'numeric_access':numeric_access,'final_numeric_access_allowed':final_numeric_access_allowed};request_pin=digest(command)
        with self._transaction() as (connection,state):
            found=_rows(connection,'SELECT * FROM rdw_approvals WHERE idempotency_key=?',(idempotency_key,))
            if found:
                _need(found[0]['request_digest']==request_pin,'idempotency_conflict');return self._current(connection,state,found[0])
            row=self._saved(connection,'rdw_assessments',assessment_id);assessment=_saved_assessment(row)
            _need(row['assessment_digest']==expected_digest,'assessment_pin_mismatch');_need(not assessment['blockers'] and assessment['status']=='ready_for_host_review','assessment_blocked')
            _need(_utc(expires_at_utc)>_utc(_now()),'approval_expired');_current_inputs(assessment)
            epoch=state['epochs'][state['settings'][SETTING_EPOCH]];projection,_=_read_identity(self.root,EXECUTION_FILE)
            _need(json_text(projection)==json_text(_projection(state,epoch)) and epoch['execution_root']==str(self.root),'workspace_identity_drift')
            value={'schema_version':1,'approval_version':AUTHORITY_VERSION,'id':uuid.uuid4().hex,'authority_id':state['anchor']['authority_id'],'epoch_id':epoch['id'],'execution_root':str(self.root),'assessment_id':assessment_id,'assessment_digest':expected_digest,'method_digest':assessment['method_digest'],'selection_digest':digest(assessment['selection']),'reviewer':reviewer,'created_at_utc':_now(),'expires_at_utc':expires_at_utc,'scope':'local_development_only','numeric_access':'development_ohlc_only','whole_byte_integrity_allowed':True,'final_numeric_access_allowed':False,'source_authenticated':False,'real_source_ready':False,'point_in_time':'not_certified'}
            connection.execute('INSERT INTO rdw_approvals VALUES (?,?,?,?,?,?,?,?,?)',(value['id'],assessment_id,value['authority_id'],epoch['id'],idempotency_key,request_pin,digest(value),json_text(value),value['created_at_utc']))
            return self._current(connection,state,self._saved(connection,'rdw_approvals',value['id']))

    def current_permission(self,approval_id,*,expected_digest):
        _pin(expected_digest)
        with self._transaction() as (connection,state):
            row=self._saved(connection,'rdw_approvals',approval_id);_need(row['approval_digest']==expected_digest,'approval_pin_mismatch');return self._current(connection,state,row)

    def revoke(self,approval_id,*,expected_digest,reason,idempotency_key):
        _pin(expected_digest);_text(reason,1000);_text(idempotency_key)
        command={'operation':'revoke','approval_id':approval_id,'expected_digest':expected_digest,'reason':reason};request_pin=digest(command)
        with self._transaction() as (connection,state):
            row=self._saved(connection,'rdw_approvals',approval_id);_need(row['approval_digest']==expected_digest,'approval_pin_mismatch')
            found=_rows(connection,'SELECT * FROM rdw_revocations WHERE idempotency_key=? OR approval_id=?',(idempotency_key,approval_id))
            if found:
                _need(len(found)==1 and found[0]['idempotency_key']==idempotency_key and found[0]['request_digest']==request_pin,'idempotency_conflict');value=_record(found[0],'revocation_digest');return {**value,'revocation_digest':found[0]['revocation_digest']}
            value={'schema_version':1,'approval_id':approval_id,'approval_digest':expected_digest,'reason':reason,'revoked_at_utc':_now()}
            connection.execute('INSERT INTO rdw_revocations VALUES (?,?,?,?,?,?)',(approval_id,idempotency_key,request_pin,digest(value),json_text(value),value['revoked_at_utc']))
            return {**value,'revocation_digest':digest(value)}

    @staticmethod
    def _reservation_summary(row,value):
        return {'id':row['id'],'reservation_id':row['id'],'reservation_digest':row['reservation_digest'],'approval_id':value['approval_id'],'approval_digest':value['approval_digest'],'assessment_id':value['assessment_id'],'assessment_digest':value['assessment_digest'],'selection_digest':value['selection_digest'],'scope_keys_digest':value['scope_keys_digest'],'status':'reserved','final_numeric_access_allowed':False,'final_numeric_rows_parsed':0}

    def reserve(self,approval_id,*,expected_digest,idempotency_key):
        _pin(expected_digest);_text(idempotency_key);command={'operation':'reserve','approval_id':approval_id,'expected_digest':expected_digest};request_pin=digest(command)
        with self._transaction() as (connection,state):
            found=_rows(connection,'SELECT * FROM rdw_reservations WHERE idempotency_key=?',(idempotency_key,))
            if found:
                _need(found[0]['request_digest']==request_pin,'idempotency_conflict');return self._reservation_summary(found[0],_record(found[0],'reservation_digest'))
            row=self._saved(connection,'rdw_approvals',approval_id);_need(row['approval_digest']==expected_digest,'approval_pin_mismatch')
            current=self._current(connection,state,row);_need(current['local_development_execution_allowed'],'approval_'+current['current_permission'])
            approval=_record(row,'approval_digest');selection=state['assessments'][approval['assessment_id']]['selection']
            reason=check_main_real_overlap(connection,assets=selection['assets'],development_sessions=selection['development_sessions'],test_sessions=selection['partitions']['test']);_need(reason is None,'main_scope_overlap')
            from ..real_daily_admission import check_real_overlap
            reason=check_real_overlap(connection,assets=selection['assets'],development_sessions=selection['development_sessions'],test_sessions=selection['partitions']['test']);_need(reason is None,'legacy_real_scope_overlap')
            from .daily_snapshot_guard import metadata_scope_history
            assets=set(selection['assets']);dev=set(selection['development_sessions']);test=set(selection['partitions']['test'])
            for identity,dates,other_assets in metadata_scope_history(connection):
                _need(not(assets&set(other_assets) and(dev&dates['test'] or test&dates['development'])),'legacy_scope_overlap')
            value={'schema_version':1,'reservation_version':AUTHORITY_VERSION,'id':uuid.uuid4().hex,'authority_id':approval['authority_id'],'epoch_id':approval['epoch_id'],'execution_root':approval['execution_root'],'approval_id':approval_id,'approval_digest':expected_digest,'assessment_id':approval['assessment_id'],'assessment_digest':approval['assessment_digest'],'selection_digest':approval['selection_digest'],'scope_keys_digest':digest(_keys(selection)),'created_at_utc':_now(),'final_numeric_access_allowed':False}
            connection.execute('INSERT INTO rdw_reservations VALUES (?,?,?,?,?,?,?,?,?)',(value['id'],approval_id,value['authority_id'],value['epoch_id'],idempotency_key,request_pin,digest(value),json_text(value),value['created_at_utc']))
            connection.executemany('INSERT INTO rdw_scope_keys VALUES (?,?,?,?)',((value['id'],*key) for key in _keys(selection)))
            _history(connection)
            return self._reservation_summary(self._saved(connection,'rdw_reservations',value['id']),value)

    def get_reservation(self,reservation_id):
        with self._transaction() as (connection,state):
            row=self._saved(connection,'rdw_reservations',reservation_id);return self._reservation_summary(row,_record(row,'reservation_digest'))
