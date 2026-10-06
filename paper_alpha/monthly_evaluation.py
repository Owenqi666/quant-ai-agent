"""Controlled-fixture monthly portfolios with frozen formation weights."""
from __future__ import annotations
from datetime import date, timedelta
import math
import statistics
from . import research_protocol as signals
from .storage import digest

SEMANTICS_VERSION = 'monthly-portfolio-v1'
STRATEGIES = ('momentum', 'mom_id')
WARNINGS = [
    'controlled_fixture_only: Fictional calendar/returns; no real-market backtest or original-paper replication.',
    'project_comparison: Both strategies have gross exposure 1 and net exposure 0; nine cells are long-only diagnostics.',
    'target_weight_turnover_proxy: Costs use changes in target weights, ignoring holding drift, financing, borrow fees and slippage.',
    'complete_holding_labels: Holdings require every declared session return; signal available rules never relax holding coverage.',
    'summary_denominators: months_evaluated counts net returns; gross_months and turnover_months count their separately available observations.',
]

class MonthlyError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'{code}: {message}')

def _months(start, end):
    while start <= end:
        yield start
        start = signals._shift_month(start, 1)

def validate_config(config):
    try:
        signals._object(config, {'schema_version', 'start_month', 'end_month', 'cost_bps', 'min_assets'}, 'monthly config')
        signals._integer(config['schema_version'], 1, 1, 'schema_version')
        start, end = signals._month(config['start_month']), signals._month(config['end_month'])
        count = (end.year-start.year)*12 + end.month-start.month+1
        if not 1 <= count <= 24:
            raise MonthlyError('month_limit', 'Provide between 1 and 24 ordered target months.')
        cost = signals._number(config['cost_bps'], 'cost_bps')
        if not 0 <= cost <= 100:
            raise MonthlyError('cost_limit', 'cost_bps must be between 0 and 100.')
        signals._integer(config['min_assets'], 9, 128, 'min_assets')
        return {**config, 'cost_bps': cost}
    except signals.ProtocolError as exc:
        raise MonthlyError(exc.code, str(exc)) from exc

def demo_bundle(config, rules):
    config = validate_config(config)
    try:
        rules = signals.validate_config(rules)
        last = signals._month(config['end_month'])
        windows = signals.resolve_windows(rules, config['start_month'])
    except signals.ProtocolError as exc:
        raise MonthlyError(exc.code, str(exc)) from exc
    start = date.fromisoformat(min(w['start'] for k,w in windows.items() if k in ('momentum','id') and w))
    end = signals._last_day(last)
    sessions, day = [], start
    while day <= end:
        if day.weekday() < 5:
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    assets = [{'id': f'A{i:02}', 'history_start': start.isoformat()} for i in range(36)]
    assets += [{'id':'MISSING_HISTORY','history_start':start.isoformat()}, {'id':'NULL_HISTORY','history_start':start.isoformat()}]
    rows, positions = [], {}
    for session in sessions:
        month = session[:7]
        pos = positions.get(month,0)
        positions[month] = pos+1
        month_number = int(month[-2:])
        for i, asset in enumerate(assets):
            id_band, mom_band, within = (i%36)//12, ((i%36)%12)//4, i%4
            growth = .005+.005*mom_band+.002*(2-id_band)*mom_band+.0002*within+.0001*((month_number%6)-2)
            growth += .0002*((month_number%6)-2)*mom_band + .00005*((month_number%6)-2)*mom_band*(2-id_band)
            if id_band == 0:
                pattern = (.002,.002,(1+growth)/(1.002**2)-1)
            elif id_band == 1:
                pattern = ((1+growth)/.995-1,-.005,0.)
            else:
                pattern = ((1+growth)/(.995**2)-1,-.005,-.005)
            value = pattern[pos] if pos<3 else 0.
            if asset['id']=='MISSING_HISTORY' and pos==3:
                continue
            rows.append({'date':session,'asset':asset['id'],'value':None if asset['id']=='NULL_HISTORY' and pos==3 else value})
    return {'schema_version':1,'data_kind':'controlled_fixture','source_id':'fictional-monthly-portfolios-v1',
            'return_semantics':'daily_total_return_decimal',
            'calendar':{'id':'fictional-weekdays-not-an-exchange','version':'1','start':start.isoformat(),'end':end.isoformat(),'sessions':sessions},
            'assets':assets,'returns':rows}

def _terciles(values):
    ordered = sorted(values,key=lambda name:(values[name],name))
    result, left, n = {},0,len(ordered)
    while left<n:
        right=left+1
        while right<n and values[ordered[right]]==values[ordered[left]]:
            right+=1
        group=min(3,(3*(left+right))//(2*n)+1)
        result.update((name,group) for name in ordered[left:right])
        left=right
    return result

def _sum(values):
    try:
        result=math.fsum(values)
    except (OverflowError,ValueError):
        return None
    return result if math.isfinite(result) else None

def _mean(values):
    return _sum(v/len(values) for v in values) if values else None

def _weights(rows,strategy,min_assets):
    if len(rows)<min_assets:
        return {},['insufficient_eligible_assets']
    legs=[(3,None,.5),(1,None,-.5)] if strategy=='momentum' else [(3,1,.25),(1,1,-.25),(3,3,-.25),(1,3,.25)]
    result={}
    for mom_group,id_group,total in legs:
        names=[r['asset'] for r in rows if r['mom_group']==mom_group and (id_group is None or r['id_group']==id_group)]
        if not names:
            return {},[f'empty_required_group:mom={mom_group},id={id_group}']
        result.update((name,total/len(names)) for name in names)
    return result,[]

def _label(sessions,returns,asset,month,calendar):
    first=signals._month(month)
    window={'start':first.isoformat(),'end':signals._last_day(first).isoformat()}
    coverage,values=signals._coverage(sessions,returns[asset],window)
    reasons=[]
    if calendar['start']>window['start'] or calendar['end']<window['end']:
        reasons.append('holding_calendar_incomplete')
    if coverage['expected']==0:
        reasons.append('holding_no_sessions')
    if coverage['valid']!=coverage['expected']:
        reasons.append('holding_returns_missing')
    value=None
    if not reasons:
        value,numerical_reason=signals._compound(values)
        if numerical_reason:
            reasons.append('holding:'+numerical_reason)
    return {'asset':asset,'return_value':value,'coverage':coverage,'reasons':reasons}

def _strategy(identity,weights,reasons,labels,previous,state,cost_bps):
    out={'id':identity,'status':'unavailable','weights':[{'asset':name,'weight':weights[name]} for name in sorted(weights)],
         'gross_return':None,'traded_weight':None,'turnover_proxy':None,'estimated_cost':None,'net_return_proxy':None,
         'nav_proxy':None,'drawdown_proxy':None,'reasons':list(reasons)}
    if weights:
        if previous is None:
            out['reasons'].append('previous_target_unknown')
        else:
            q=math.fsum(abs(weights.get(name,0)-previous.get(name,0)) for name in sorted(set(weights)|set(previous)))
            out.update(traded_weight=q,turnover_proxy=q/2,estimated_cost=q*cost_bps/10000)
        missing=[name for name in weights if labels[name]['return_value'] is None]
        if missing:
            out['reasons'].extend('holding_label_unavailable:'+name for name in sorted(missing))
        else:
            out['gross_return']=_sum(weights[name]*labels[name]['return_value'] for name in sorted(weights))
            if out['gross_return'] is None:
                out['reasons'].append('nonfinite_portfolio_return')
        if out['gross_return'] is not None and out['estimated_cost'] is not None:
            net=out['gross_return']-out['estimated_cost']
            if math.isfinite(net):
                out.update(net_return_proxy=net,status='evaluated')
            else:
                out['reasons'].append('nonfinite_net_return')
    previous=weights if weights else None
    net=out['net_return_proxy']
    if state['nav'] is not None and net is not None and net>-1:
        nav=state['nav']*(1+net)
        if math.isfinite(nav) and nav>0:
            state['nav']=nav
            state['peak']=max(state['peak'],nav)
            out.update(nav_proxy=nav,drawdown_proxy=nav/state['peak']-1)
        else:
            state['nav']=None
            out['reasons'].append('cumulative_numeric_unavailable')
    else:
        if state['nav'] is None:
            out['reasons'].append('cumulative_path_unavailable')
        elif net is not None and net <= -1:
            out['reasons'].append('cumulative_nonpositive_gross_factor')
        state['nav']=None
    return out,previous

def _summary(months,identity):
    rows=[next(s for s in month['strategies'] if s['id']==identity) for month in months]
    gross=[r['gross_return'] for r in rows if r['gross_return'] is not None]
    net=[r['net_return_proxy'] for r in rows if r['net_return_proxy'] is not None]
    turnover=[r['turnover_proxy'] for r in rows if r['turnover_proxy'] is not None]
    volatility=sharpe=None
    if len(net)>=2:
        try:
            sd=statistics.stdev(net)
            if sd>0:
                vol,ratio=sd*math.sqrt(12),_mean(net)/sd*math.sqrt(12)
                volatility=vol if math.isfinite(vol) else None
                sharpe=ratio if math.isfinite(ratio) else None
            else:
                volatility=0.
        except (OverflowError,ValueError,ZeroDivisionError):
            pass
    complete=bool(rows) and all(row['nav_proxy'] is not None for row in rows)
    return {'strategy_id':identity,'months_total':len(rows),'months_evaluated':len(net),'gross_months':len(gross),'turnover_months':len(turnover),
            'mean_gross_return':_mean(gross),'mean_net_return_proxy':_mean(net),'volatility_annualized_proxy':volatility,
            'sharpe_annualized_proxy':sharpe,'max_drawdown_proxy':min(row['drawdown_proxy'] for row in rows) if complete else None,
            'terminal_nav_proxy':rows[-1]['nav_proxy'] if complete else None,'mean_turnover_proxy':_mean(turnover),'cumulative_complete':complete}

def evaluate(rules,config,bundle):
    config=validate_config(config)
    try:
        rules=signals.validate_config(rules)
        sessions,_,returns,_=signals._validate_bundle(bundle,signals.resolve_windows(rules,config['start_month']))
        periods=[]
        previous={identity:{} for identity in STRATEGIES}
        states={identity:{'nav':1.,'peak':1.} for identity in STRATEGIES}
        for target in _months(signals._month(config['start_month']),signals._month(config['end_month'])):
            month=target.strftime('%Y-%m')
            formed=signals.preview(rules,month,bundle)
            eligible=[r for r in formed['assets'] if r['status']=='ready']
            mom_groups=_terciles({r['asset']:r['momentum'] for r in eligible})
            id_groups=_terciles({r['asset']:r['id'] for r in eligible})
            rows=[{'asset':r['asset'],'momentum':r['momentum'],'pret':r['pret'],'id':r['id'],
                   'mom_group':mom_groups[r['asset']],'id_group':id_groups[r['asset']]} for r in eligible]
            labels={r['asset']:_label(sessions,returns,r['asset'],month,bundle['calendar']) for r in rows}
            groups=[]
            for m in range(1,4):
                for i in range(1,4):
                    names=sorted(r['asset'] for r in rows if r['mom_group']==m and r['id_group']==i)
                    reasons=['empty_group'] if not names else ['holding_label_unavailable:'+name for name in names if labels[name]['return_value'] is None]
                    value=None if reasons else _mean([labels[name]['return_value'] for name in names])
                    if names and not reasons and value is None:
                        reasons.append('nonfinite_group_return')
                    groups.append({'mom_group':m,'id_group':i,'assets':names,'gross_return':value,'reasons':reasons})
            strategies=[]
            for identity in STRATEGIES:
                weights,reasons=_weights(rows,identity,config['min_assets'])
                if rules['mode']=='paper':
                    reasons=['paper_id_rules_unresolved']
                strategy,previous[identity]=_strategy(identity,weights,reasons,labels,previous[identity],states[identity],config['cost_bps'])
                strategies.append(strategy)
            status='evaluated' if all(s['status']=='evaluated' for s in strategies) else 'partial' if any(s['gross_return'] is not None for s in strategies) else 'unavailable'
            difference=None
            if all(s['net_return_proxy'] is not None for s in strategies):
                difference=_sum([strategies[1]['net_return_proxy'],-strategies[0]['net_return_proxy']])
            periods.append({'month':month,'as_of':formed['windows']['as_of'],'windows':formed['windows'],
                            'status':'blocked' if rules['mode']=='paper' else status,'eligible_assets':[r['asset'] for r in rows],
                            'exclusions':[{'asset':r['asset'],'reasons':r['reasons']} for r in formed['assets'] if r['status']!='ready'],
                            'signals':rows,'groups':groups,'labels':list(labels.values()),'strategies':strategies,
                            'difference_net_proxy':difference,'warnings':formed['warnings']+['difference_net_proxy is mom_id minus momentum on the same formed sample.']})
    except signals.ProtocolError as exc:
        raise MonthlyError(exc.code,str(exc)) from exc
    status='evaluated' if all(m['status']=='evaluated' for m in periods) else 'partial' if any(s['gross_return'] is not None for m in periods for s in m['strategies']) else 'not_evaluable'
    return {'schema_version':1,'semantics_version':SEMANTICS_VERSION,'data_kind':'controlled_fixture','source_id':bundle['source_id'],
            'rules':rules,'config':config,'config_digest':digest({'rules':rules,'config':config}),'input_digest':digest(bundle),
            'status':'blocked' if rules['mode']=='paper' else status,'warnings':list(WARNINGS),'months':periods,
            'summary':[_summary(periods,identity) for identity in STRATEGIES]}
