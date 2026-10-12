"""Independent input-to-metrics oracle, deliberately not importing production calculators.

Exact binary64 rational products, pairwise rank counts, independently reconstructed
weights and state transitions. Hash encoding is shared, arithmetic is not.
"""
from __future__ import annotations
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
import math
import json
import re
from .storage import digest

IDS = ('momentum', 'mom_id')
_UNRESOLVED = [
    'id_daily_window_unresolved: The author code selects DGW at target month minus one, but its internal daily window is not published.',
    'id_missing_policy_unresolved: Daily missing-value and minimum-observation rules are not confirmed.',
]
_WARNINGS = [
    'controlled_fixture_only: Fictional calendar/returns; no real-market backtest or original-paper replication.',
    'project_comparison: Both strategies have gross exposure 1 and net exposure 0; nine cells are long-only diagnostics.',
    'target_weight_turnover_proxy: Costs use changes in target weights, ignoring holding drift, financing, borrow fees and slippage.',
    'complete_holding_labels: Holdings require every declared session return; signal available rules never relax holding coverage.',
    'summary_denominators: months_evaluated counts net returns; gross_months and turnover_months count their separately available observations.',
]

def _warnings(rules):
    result = ['controlled_fixture_only: Signal and coverage diagnostics; no real-market backtest or portfolio performance is calculated.',
              'calendar_declared: Complete sessions and return adjustment semantics are input declarations, not externally verified market facts.']
    if rules['mode']=='paper': result += _UNRESOLVED
    else:
        result.append('project_adaptation: The executable daily-window and missing-data choices are project rules, not verified original-paper rules.')
        if rules['missing_policy']=='available':
            result.append('observed_returns_only: available compounds only observed valid daily returns; with missing observations this is not the complete-window return. Missing returns are not filled, and missing days do not enter the ID denominator.')
    return result + ['difference_net_proxy is mom_id minus momentum on the same formed sample.']

def _inputs(rules, config, bundle):
    """Own admission checks: no calculator imports and no unbounded month loop."""
    def fields(value, names):
        if not isinstance(value, dict) or set(value) != set(names.split()):
            raise ValueError('reference input has invalid fields')
    def integer(value, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('reference integer bounds exceeded')
    def number(value, low, high):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('reference numeric bounds exceeded')
    def text(value, maximum=256):
        if not isinstance(value, str) or not value.strip() or len(value)>maximum:
            raise ValueError('reference text bounds exceeded')
    def day(value):
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('reference date is invalid')
        return date.fromisoformat(value)
    fields(config, 'schema_version start_month end_month cost_bps min_assets')
    integer(config['schema_version'],1,1)
    dates=[]
    for key in ('start_month','end_month'):
        value=config[key]
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}',value):
            raise ValueError('reference month is invalid')
        parsed=day(value+'-01'); integer(parsed.year,1905,2099); dates.append(parsed)
    integer((dates[1].year-dates[0].year)*12+dates[1].month-dates[0].month+1,1,24)
    integer(config['min_assets'],9,128); number(config['cost_bps'],0,100)
    fields(rules, 'schema_version mode mom_window_months mom_skip_months id_window_months id_skip_months missing_policy min_coverage max_missing_run zero_policy fill_policy')
    integer(rules['schema_version'],1,1)
    if rules['mode'] not in ('project','paper') or rules['zero_policy']!='include' or rules['fill_policy']!='none':
        raise ValueError('reference policies are invalid')
    integer(rules['mom_window_months'],1,36); integer(rules['mom_skip_months'],0,12)
    if rules['mode']=='paper':
        if (rules['mom_window_months']!=11 or rules['mom_skip_months']!=1 or rules['id_window_months'] is not None or
                rules['id_skip_months'] is not None or rules['min_coverage'] is not None or
                rules['max_missing_run'] is not None or rules['missing_policy']!='unresolved'):
            raise ValueError('reference paper profile is modified')
    else:
        integer(rules['id_window_months'],1,36); integer(rules['id_skip_months'],0,12)
        integer(rules['max_missing_run'],0,366); number(rules['min_coverage'],0,1)
        if (rules['min_coverage']==0 or rules['missing_policy'] not in ('complete','available') or
                (rules['missing_policy']=='complete' and (rules['min_coverage']!=1 or rules['max_missing_run']!=0))):
            raise ValueError('reference missing policies are inconsistent')
    fields(bundle, 'schema_version data_kind source_id return_semantics calendar assets returns')
    integer(bundle['schema_version'],1,1); text(bundle['source_id'])
    if bundle['data_kind']!='controlled_fixture' or bundle['return_semantics']!='daily_total_return_decimal':
        raise ValueError('reference accepts declared fixture total returns only')
    cal=bundle['calendar']; fields(cal,'id version start end sessions'); text(cal['id']); text(cal['version'])
    start,end=day(cal['start']),day(cal['end']); integer((end-start).days+1,1,7320)
    sessions=cal['sessions']; assets=bundle['assets']; rows=bundle['returns']
    if not isinstance(sessions,list) or not isinstance(assets,list) or not isinstance(rows,list):
        raise ValueError('reference arrays required')
    integer(len(sessions),1,6000); integer(len(assets),1,128); integer(len(rows),0,100000)
    integer(len(sessions)*len(assets),1,300000)
    previous=None
    for session in sessions:
        parsed=day(session)
        if not start<=parsed<=end or (previous is not None and session<=previous):
            raise ValueError('reference sessions must be ordered and unique')
        previous=session
    for target in (dates[0],dates[1]):
        for prefix in ('mom','id'):
            if rules[prefix+'_window_months'] is None: continue
            span=_window(target,rules[prefix+'_window_months'],rules[prefix+'_skip_months'])
            if cal['start']>span['start'] or cal['end']<span['end']:
                raise ValueError('reference formation calendar is incomplete')
    names={}
    for asset in assets:
        fields(asset,'id history_start'); text(asset['id'],128); day(asset['history_start'])
        if asset['id'] in names: raise ValueError('reference duplicate asset')
        names[asset['id']]=asset['history_start']
    seen=set(); known=set(sessions)
    for row in rows:
        fields(row,'asset date value'); day(row['date'])
        if not isinstance(row['asset'],str) or row['asset'] not in names or row['date'] not in known:
            raise ValueError('reference unknown asset or session')
        key=(row['asset'],row['date'])
        if key in seen or row['date']<names[row['asset']]: raise ValueError('reference duplicate or early return')
        seen.add(key)
        if row['value'] is not None: number(row['value'],-1,float('inf'))
    if len((json.dumps(bundle,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf8'))>16*1024*1024:
        raise ValueError('reference bundle size limit exceeded')

def _move(d, offset):
    y, m = divmod(d.year*12+d.month-1+offset, 12)
    return date(y,m+1,1)

def _end(d):
    return date(d.year,d.month,monthrange(d.year,d.month)[1])

def _window(d,n,skip):
    stop = _move(d,-skip-1)
    return {'start':_move(stop,1-n).isoformat(),'end':_end(stop).isoformat()}

def _float(x):
    try:
        value=float(x)
    except (OverflowError,ValueError):
        return None
    return value if math.isfinite(value) else None

def _product(values):
    if any(v == -1 for v in values):
        return -1., None
    total=Fraction(1)
    # Repeated fixture values are compressed, unlike production log reduction.
    for value,count in Counter(values).items():
        if value:
            factor=Fraction.from_float(float(value))+1
            total*=factor**count
            if total.numerator.bit_length()+total.denominator.bit_length()>2000000:
                raise ValueError('reference rational arithmetic budget exceeded')
    difference=total-1
    value=_float(difference)
    if value is None:
        return None,'nonfinite_compound'
    if difference and value == 0:
        return None,'compound_precision_underflow'
    return value,None

def _coverage(calendar, records, asset, span):
    days=[s for s in calendar['sessions'] if span['start']<=s<=span['end']]
    values=[]; missing=[]; runs=[]; run=0
    for day in days:
        key=(asset,day)
        if key not in records or records[key] is None:
            missing.append({'date':day,'reason':'missing_row' if key not in records else 'null_value'})
            run+=1
        else:
            runs.append(run); run=0
            values.append(float(records[key]))
    runs.append(run)
    n=len(days); k=len(values)
    return {'expected':n,'valid':k,'positive':sum(v>0 for v in values),'negative':sum(v<0 for v in values),
            'zero':sum(v==0 for v in values),'missing_rows':sum(x['reason']=='missing_row' for x in missing),
            'null_values':sum(x['reason']=='null_value' for x in missing),'coverage':k/n if n else 0.,
            'max_missing_run':max(runs,default=0),'missing_dates':missing}, values

def _signal(rules,calendar,records,asset,span,history,field):
    c,values=_coverage(calendar,records,asset,span)
    failures=[]
    if history>span['start']: failures.append(field+':history_insufficient')
    if not c['expected']: failures.append(field+':no_expected_sessions')
    if not c['valid']: failures.append(field+':no_valid_returns')
    if c['coverage']<rules['min_coverage']: failures.append(field+':coverage_below_minimum')
    if c['max_missing_run']>rules['max_missing_run']: failures.append(field+':missing_run_exceeded')
    if failures: return None,c,failures
    value,problem=_product(values)
    if problem: failures.append(field+':'+problem)
    return value,c,failures

def _groups(values):
    # Pairwise counts are independent of the production sorted-block procedure.
    n=len(values)
    return {a:min(3, (3*(2*sum(other<v for other in values.values())+sum(other==v for other in values.values())))//(2*n)+1)
            for a,v in values.items()}

def _average(values):
    return _float(sum((Fraction.from_float(v) for v in values),Fraction())/len(values)) if values else None

def _label(calendar,records,asset,month):
    span={'start':month.isoformat(),'end':_end(month).isoformat()}
    c,values=_coverage(calendar,records,asset,span)
    problems=[]
    if calendar['start']>span['start'] or calendar['end']<span['end']: problems.append('holding_calendar_incomplete')
    if not c['expected']: problems.append('holding_no_sessions')
    if c['valid']!=c['expected']: problems.append('holding_returns_missing')
    value=None
    if not problems:
        value,error=_product(values)
        if error: problems.append('holding:'+error)
    return {'asset':asset,'return_value':value,'coverage':c,'reasons':problems}

def _portfolio(rows,identity,minimum):
    if len(rows)<minimum: return {},['insufficient_eligible_assets']
    if identity=='momentum':
        specifications=[(3,None,Fraction(1,2)),(1,None,Fraction(-1,2))]
    else:
        specifications=[(3,1,Fraction(1,4)),(1,1,Fraction(-1,4)),(3,3,Fraction(-1,4)),(1,3,Fraction(1,4))]
    weights={}
    for mg,ig,budget in specifications:
        members={r['asset'] for r in rows if r['mom_group']==mg and (ig is None or r['id_group']==ig)}
        if not members: return {},[f'empty_required_group:mom={mg},id={ig}']
        for asset in members: weights[asset]=float(budget/len(members))
    return weights,[]

def _strategy(identity, weights, problems, labels, past, nav, peak, fee):
    q=cost=gross=net=dd=next_nav=None
    reasons=list(problems)
    if weights:
        if past is None:
            reasons.append('previous_target_unknown')
        else:
            q=float(sum((abs(Fraction(weights.get(a,0))-Fraction(past.get(a,0))) for a in set(weights)|set(past)),Fraction()))
            cost=q*fee/10000
        missing=sorted(a for a in weights if labels[a]['return_value'] is None)
        if missing:
            reasons += ['holding_label_unavailable:'+a for a in missing]
        else:
            gross=_float(sum((Fraction(weights[a])*Fraction(labels[a]['return_value']) for a in weights),Fraction()))
            if gross is None: reasons.append('nonfinite_portfolio_return')
        if gross is not None and cost is not None:
            net=_float(Fraction(gross)-Fraction(cost))
            if net is None: reasons.append('nonfinite_net_return')
    if nav is not None and net is not None and net>-1:
        candidate=nav*(1+net)
        if math.isfinite(candidate) and candidate>0:
            next_nav=candidate; peak=max(peak,candidate); dd=candidate/peak-1
        else: reasons.append('cumulative_numeric_unavailable')
    elif nav is None:
        reasons.append('cumulative_path_unavailable')
    elif net is not None and net <= -1:
        reasons.append('cumulative_nonpositive_gross_factor')
    result={'id':identity,'status':'evaluated' if net is not None else 'unavailable',
            'weights':[{'asset':a,'weight':weights[a]} for a in sorted(weights)],'gross_return':gross,
            'traded_weight':q,'turnover_proxy':None if q is None else q/2,'estimated_cost':cost,'net_return_proxy':net,
            'nav_proxy':next_nav,'drawdown_proxy':dd,'reasons':reasons}
    return result,(weights if weights else None),next_nav,peak

def _metric(months,identity):
    rows=[next(s for s in m['strategies'] if s['id']==identity) for m in months]
    gross=[x['gross_return'] for x in rows if x['gross_return'] is not None]
    net=[x['net_return_proxy'] for x in rows if x['net_return_proxy'] is not None]
    turns=[x['turnover_proxy'] for x in rows if x['turnover_proxy'] is not None]
    vol=sharpe=None
    if len(net)>=2:
        exact=[Fraction(v) for v in net]; mean=sum(exact,Fraction())/len(exact)
        variance=sum(((v-mean)**2 for v in exact),Fraction())/(len(exact)-1)
        if variance:
            with localcontext() as ctx:
                ctx.prec=60
                sd=(Decimal(variance.numerator)/Decimal(variance.denominator)).sqrt()
                vol=_float(sd*Decimal(12).sqrt())
                sharpe=_float((Decimal(mean.numerator)/Decimal(mean.denominator))/sd*Decimal(12).sqrt())
        else: vol=0.
    complete=all(x['nav_proxy'] is not None for x in rows)
    return {'strategy_id':identity,'months_total':len(rows),'months_evaluated':len(net),'gross_months':len(gross),
            'turnover_months':len(turns),'mean_gross_return':_average(gross),'mean_net_return_proxy':_average(net),
            'volatility_annualized_proxy':vol,'sharpe_annualized_proxy':sharpe,
            'max_drawdown_proxy':min(x['drawdown_proxy'] for x in rows) if complete else None,
            'terminal_nav_proxy':rows[-1]['nav_proxy'] if complete else None,'mean_turnover_proxy':_average(turns),
            'cumulative_complete':complete}

def _expected(rules,config,bundle):
    calendar=bundle['calendar']; records={(r['asset'],r['date']):r['value'] for r in bundle['returns']}
    assets=sorted(bundle['assets'],key=lambda x:x['id'])
    months=[]; prior={s:{} for s in IDS}; nav={s:1. for s in IDS}; peak={s:1. for s in IDS}
    current=date.fromisoformat(config['start_month']+'-01'); stop=date.fromisoformat(config['end_month']+'-01')
    while current<=stop:
        key=current.strftime('%Y-%m'); spans={'target_month':key,'as_of':(current-timedelta(days=1)).isoformat(),
          'momentum':_window(current,rules['mom_window_months'],rules['mom_skip_months']),
          'id':_window(current,rules['id_window_months'],rules['id_skip_months'])}
        rows=[]; exclusions=[]
        for asset in assets:
            a=asset['id']; history=asset['history_start']
            momentum,_,mp=_signal(rules,calendar,records,a,spans['momentum'],history,'momentum')
            pret,c,ip=_signal(rules,calendar,records,a,spans['id'],history,'id')
            if momentum is None or pret is None:
                exclusions.append({'asset':a,'reasons':mp+ip})
            else:
                identifier=((pret>0)-(pret<0))*(c['negative']-c['positive'])/c['valid']
                rows.append({'asset':a,'momentum':momentum,'pret':pret,'id':identifier})
        mg=_groups({r['asset']:r['momentum'] for r in rows}); ig=_groups({r['asset']:r['id'] for r in rows})
        for row in rows: row.update(mom_group=mg[row['asset']],id_group=ig[row['asset']])
        labels={r['asset']:_label(calendar,records,r['asset'],current) for r in rows}
        groups=[]
        for m in range(1,4):
            for i in range(1,4):
                names=sorted(r['asset'] for r in rows if r['mom_group']==m and r['id_group']==i)
                reasons=['empty_group'] if not names else ['holding_label_unavailable:'+a for a in names if labels[a]['return_value'] is None]
                ret=None if reasons else _average([labels[a]['return_value'] for a in names])
                if names and not reasons and ret is None: reasons.append('nonfinite_group_return')
                groups.append({'mom_group':m,'id_group':i,'assets':names,'gross_return':ret,'reasons':reasons})
        strategies=[]
        for identity in IDS:
            weights,problems=_portfolio(rows,identity,config['min_assets'])
            strategy,prior[identity],nav[identity],peak[identity]=_strategy(identity,weights,problems,labels,prior[identity],nav[identity],peak[identity],config['cost_bps'])
            strategies.append(strategy)
        net=[s['net_return_proxy'] for s in strategies]
        status='evaluated' if all(v is not None for v in net) else 'partial' if any(s['gross_return'] is not None for s in strategies) else 'unavailable'
        difference=_float(Fraction(net[1])-Fraction(net[0])) if all(x is not None for x in net) else None
        months.append({'month':key,'as_of':spans['as_of'],'windows':spans,'status':status,
                       'eligible_assets':[r['asset'] for r in rows],'exclusions':exclusions,'signals':rows,
                       'groups':groups,'labels':list(labels.values()),'strategies':strategies,'difference_net_proxy':difference,
                       'warnings':_warnings(rules)})
        current=_move(current,1)
    state='evaluated' if all(m['status']=='evaluated' for m in months) else 'partial' if any(s['gross_return'] is not None for m in months for s in m['strategies']) else 'not_evaluable'
    normalized_config={**config,'cost_bps':float(config['cost_bps'])}
    normalized_rules={**rules,'min_coverage':float(rules['min_coverage'])}
    return {'schema_version':1,'semantics_version':'monthly-portfolio-v1','data_kind':'controlled_fixture','source_id':bundle['source_id'],
            'rules':normalized_rules,'config':normalized_config,'config_digest':digest({'rules':normalized_rules,'config':normalized_config}),
            'input_digest':digest(bundle),'status':state,'warnings':list(_WARNINGS),'months':months,'summary':[_metric(months,s) for s in IDS]}

def _blocked(rules,config,bundle):
    """Validate that unresolved paper identity never acquires numerical results."""
    months=[]; current=date.fromisoformat(config['start_month']+'-01'); stop=date.fromisoformat(config['end_month']+'-01')
    while current<=stop:
        key=current.strftime('%Y-%m'); as_of=(current-timedelta(days=1)).isoformat()
        strategies=[]
        for identity in IDS:
            strategies.append({'id':identity,'status':'unavailable','weights':[],
                **dict.fromkeys(('gross_return','traded_weight','turnover_proxy','estimated_cost','net_return_proxy','nav_proxy','drawdown_proxy')),
                'reasons':['paper_id_rules_unresolved']+(['cumulative_path_unavailable'] if months else [])})
        months.append({'month':key,'as_of':as_of,'windows':{'target_month':key,'as_of':as_of,
             'momentum':_window(current,11,1),'id':None},'status':'blocked','eligible_assets':[],
             'exclusions':[{'asset':a['id'],'reasons':list(_UNRESOLVED)} for a in sorted(bundle['assets'],key=lambda a:a['id'])],
             'signals':[],'groups':[{'mom_group':m,'id_group':i,'assets':[],'gross_return':None,'reasons':['empty_group']}
                                    for m in range(1,4) for i in range(1,4)],
             'labels':[],'strategies':strategies,'difference_net_proxy':None,'warnings':_warnings(rules)})
        current=_move(current,1)
    normalized={**config,'cost_bps':float(config['cost_bps'])}
    return {'schema_version':1,'semantics_version':'monthly-portfolio-v1','data_kind':'controlled_fixture',
            'source_id':bundle['source_id'],'rules':rules,'config':normalized,
            'config_digest':digest({'rules':rules,'config':normalized}),'input_digest':digest(bundle),
            'status':'blocked','warnings':list(_WARNINGS),'months':months,'summary':[_metric(months,s) for s in IDS]}

def check(rules,config,bundle,result):
    supported=not (isinstance(rules,dict) and rules.get('mode')=='paper')
    issues=[]; checks=0
    def compare(expected, actual, path):
        nonlocal checks
        if len(issues)>=50: return
        if isinstance(expected,dict):
            if not isinstance(actual,dict): issues.append(path+': object required'); return
            if set(expected)!=set(actual): issues.append(path+': object fields mismatch')
            for key,value in expected.items():
                if key not in actual: issues.append(path+'.'+key+': missing')
                else: compare(value,actual[key],path+'.'+key)
        elif isinstance(expected,list):
            if not isinstance(actual,list) or len(expected)!=len(actual): issues.append(path+': list length mismatch'); return
            for index,(left,right) in enumerate(zip(expected,actual)): compare(left,right,f'{path}[{index}]')
        elif type(expected) is float:
            checks+=1
            if type(actual) not in (int,float) or not math.isfinite(actual) or not math.isclose(expected,actual,rel_tol=2e-10,abs_tol=2e-12):
                issues.append(path+': numeric mismatch')
        else:
            checks+=1
            if type(expected) is not type(actual) or expected!=actual: issues.append(path+': value mismatch')
    try:
        _inputs(rules,config,bundle)
        expected=_expected(rules,config,bundle) if supported else _blocked(rules,config,bundle)
        compare(expected,result,'result')
    except (ValueError,TypeError,KeyError,OverflowError,ZeroDivisionError) as exc:
        issues.append('reference calculation failed: '+str(exc)[:300])
    return {'supported':supported,'passed':not issues if supported else None,'checks':checks,'issues':issues}
