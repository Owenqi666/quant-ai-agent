"""Prepare a NEW synthetic main-lane workspace; no market/model credentials.

All symbols and source facts are engineering fixtures. This creates inputs and
metadata handles, never approvals, reservations, numeric runs or reviews.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .storage import json_text
from .real_daily_admission import FACTS
from .real_daily_method import method_digest
from .daily_event_review import EVENT_TYPES, VERSION as EVENT_VERSION
from .server.real_daily_prefix_schema import CAPTURE_VERSION
from .server.service import Store

ASSETS = ('AAPL','MSFT','NVDA')
DAYS = ('2024-01-02','2024-01-03','2024-01-04','2024-01-05','2024-01-08','2024-01-09',
        '2024-01-10','2024-01-11','2024-01-12','2024-01-15','2024-01-16','2024-01-17')


def selection():
    return {'assets':list(ASSETS),'sessions':list(DAYS),'development_sessions':list(DAYS[:9]),
        'partitions':{'train':list(DAYS[:3]),'validation':list(DAYS[3:9]),'test':list(DAYS[9:])},
        'config':{'min_assets':3,'splits':{name:{'start':dates[0],'end':dates[-1]} for name,dates in
            (('train',DAYS[:3]),('validation',DAYS[3:9]),('test',DAYS[9:]))}}}


def _capture(store,capture_id,*,unknown_units=False):
    folder=store.root/'real_daily_sources'/capture_id
    (folder/'raw').mkdir(parents=True,exist_ok=False)
    note=folder/'evidence.json'
    note.write_text(json_text({'kind':'synthetic_engineering_metadata','no_real_source_authority':True}),encoding='utf-8')
    pin=hashlib.sha256(note.read_bytes()).hexdigest();raw=[]
    for n,asset in enumerate(ASSETS):
        bars={}
        for i,day in enumerate(DAYS[:9]):
            opening=100+i*(n+1)/10
            bars[day]={'1. open':str(opening),'2. high':str(opening+2),'3. low':str(opening-2),
                '4. close':str(opening+(-.7,.1,.9)[n]),'5. volume':{'opaque':'VOLUME_POISON'}}
        for day in DAYS[9:]:
            bars[day]={'1. open':'FINAL_POISON','2. high':{'nested':[None,'invalid-quote']},
                '3. low':10**400,'4. close':'\\uZZZZ','5. volume':'not-volume'}
        payload={'Meta Data':{'2. Symbol':asset},'Time Series (Daily)':bars}
        target=folder/'raw'/(asset+'.raw.json');target.write_text(json.dumps(payload),encoding='utf-8')
        data=target.read_bytes();raw.append({'asset':asset,'path':str(target),'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data)})
    facts={name:{'status':'supported','statement':'Synthetic engineering declaration only: '+name,'evidence_sha256':pin} for name in FACTS}
    if unknown_units:facts['units']={'status':'unknown','statement':None,'evidence_sha256':None}
    ledger={'schema_version':1,'ledger_version':EVENT_VERSION,'ledger_id':capture_id+'-events',
        'created_at_utc':'2026-10-11T00:00:00Z','selection':{'assets':list(ASSETS),'sessions':list(DAYS)},
        'evidence':[{'id':'scope-01','url':'https://issuer.example.com/engineering-fixture',
            'retrieved_at_utc':'2026-10-11T00:00:00Z','published_date':'2026-10-10',
            'locator':'Synthetic coverage declaration','excerpt':'Synthetic fixture only.',
            'statement':'No financial source authority.',
            'applicability':{'assets':list(ASSETS),'event_types':list(EVENT_TYPES)}}],
        'coverage':[{'asset':asset,'event_type':kind,'status':'declared_complete',
            'statement':'Synthetic complete coverage declaration.','evidence_refs':['scope-01']}
            for asset in ASSETS for kind in EVENT_TYPES],'events':[]}
    request={'schema_version':1,'selection':selection(),'ledger':ledger,'cash_annotations':[],
        'source_facts':facts,'method_digest':method_digest(),'input_pins':{'origin':'engineering_fixture',
            'metadata':[{'path':str(note),'sha256':pin}],'raw':raw,'code':[]}}
    packet={'schema_version':1,'capture_version':CAPTURE_VERSION,'capture_id':capture_id,'request':request}
    (folder/'capture.json').write_text(json_text(packet),encoding='utf-8')
    return store.real_daily_sources().capture(capture_id,idempotency_key=capture_id+'-capture')


def prepare(home):
    home=Path(home).absolute()
    if home.exists() or home.is_symlink():raise ValueError('A NEW isolated workspace is required')
    home.mkdir(parents=True,exist_ok=False)
    store=Store(home);store.enable_real_daily_authority()
    example=store.seed_example()
    handles=[_capture(store,'synthetic-alpha101-ready'),_capture(store,'synthetic-alpha101-unknown-units',unknown_units=True)]
    return {'schema_version':1,'status':'prepared_not_approved','home':str(home),'origin':'engineering_fixture',
        'template_research_id':example['research_id'],'handles':[{'id':h['id'],'handle_digest':h['handle_digest'],
            'capture_id':capture_id,'source_fact_counts':h['source_fact_counts']} for capture_id,h in zip(('synthetic-alpha101-ready','synthetic-alpha101-unknown-units'),handles)],
        'development_cells':27,'retained_final_cells':9,'approvals':0,'reservations':0,'numeric_runs':0,
        'native_calls':0,'market_calls':0,'real_raw_reads':0,'final_numeric_values_decoded':0,
        'source_authenticated':False,'real_source_ready':False,'point_in_time':'not_certified'}


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='operation',required=True)
    p=commands.add_parser('prepare');p.add_argument('--home',required=True,type=Path)
    args=parser.parse_args(argv)
    print(json_text(prepare(args.home)),end='');return 0


if __name__=='__main__':raise SystemExit(main())
