from copy import deepcopy
from fractions import Fraction
import json
import math
import unittest
from paper_alpha import monthly_evaluation as m, monthly_reference as reference, research_protocol as protocol


def rules(**changes):
    result=protocol.presets()[1]['config']
    result.update(mom_window_months=1,id_window_months=1)
    result.update(changes)
    return result


def config(**changes):
    result={'schema_version':1,'start_month':'2026-07','end_month':'2026-07','cost_bps':10,'min_assets':9}
    result.update(changes)
    return result


def hand_bundle(end='2026-08', target_labels=True):
    months=['2026-05','2026-06','2026-07','2026-08','2026-09','2026-10']
    months=[month for month in months if month<=end]
    sessions=[month+suffix for month in months for suffix in ('-01','-02','-03')]
    returns=[]
    labels=[[-.02,0.,.10],[-.01,.02,.07],[.01,.01,.03]]
    for month in months:
        for ig in range(3):
            for mg in range(3):
                gross=[8,16,32][mg]+ig
                pattern=[1.,1.,gross/4-1] if ig==0 else [2*gross-1,-.5,0.] if ig==1 else [4*gross-1,-.5,-.5]
                if target_labels and month>='2026-07': pattern=[labels[ig][mg],0.,0.]
                for day,value in zip(('-01','-02','-03'),pattern):
                    returns.append({'date':month+day,'asset':f'A{ig}{mg}','value':value})
    # Explicit three-session fictional months make all products hand-computable.
    return {'schema_version':1,'data_kind':'controlled_fixture','source_id':'nine-assets-hand-v1',
            'return_semantics':'daily_total_return_decimal','calendar':{'id':'three-session-fictional','version':'1',
            'start':'2026-05-01','end':end+'-31','sessions':sessions},
            'assets':[{'id':f'A{i}{j}','history_start':'2026-05-01'} for i in range(3) for j in range(3)],'returns':returns}


def strategy(result, identity, month=0):
    return next(s for s in result['months'][month]['strategies'] if s['id']==identity)


class MonthlyEvaluationTests(unittest.TestCase):
    def run_checked(self,bundle=None,options=None,rule_set=None):
        b=bundle or hand_bundle(); c=options or config(); r=rule_set or rules()
        value=m.evaluate(r,c,b)
        checked=reference.check(r,c,b,value)
        self.assertTrue(checked['passed'],checked)
        self.assertGreater(checked['checks'],100)
        json.dumps(value,allow_nan=False)
        return value

    def test_hand_nine_cells_gross_exposure_cost_and_same_sample(self):
        result=self.run_checked()
        month=result['months'][0]
        self.assertEqual(result['status'],'evaluated')
        self.assertEqual(len(month['eligible_assets']),9)
        self.assertTrue(all(len(group['assets'])==1 for group in month['groups']))
        baseline=strategy(result,'momentum'); conditional=strategy(result,'mom_id')
        self.assertAlmostEqual(baseline['gross_return'],float(Fraction(11,300)),places=14)
        self.assertAlmostEqual(conditional['gross_return'],.025,places=14)
        for row in (baseline,conditional):
            self.assertEqual(sum(abs(w['weight']) for w in row['weights']),1.)
            self.assertAlmostEqual(sum(w['weight'] for w in row['weights']),0)
            self.assertEqual(row['traded_weight'],1.)
            self.assertEqual(row['turnover_proxy'],.5)
            self.assertEqual(row['estimated_cost'],.001)
            self.assertAlmostEqual(row['net_return_proxy'],row['gross_return']-.001)
            self.assertAlmostEqual(row['nav_proxy'],1+row['net_return_proxy'])
        self.assertAlmostEqual(month['difference_net_proxy'],.025-float(Fraction(11,300)))

    def test_two_month_target_proxy_charges_entry_not_constant_targets(self):
        result=self.run_checked(options=config(end_month='2026-08'))
        for identity in m.STRATEGIES:
            first=strategy(result,identity); second=strategy(result,identity,1)
            self.assertEqual(first['weights'],second['weights'])
            self.assertEqual(second['traded_weight'],0.)
            self.assertEqual(second['estimated_cost'],0.)
            self.assertAlmostEqual(second['nav_proxy'],(1+first['net_return_proxy'])*(1+second['net_return_proxy']))
            summary=next(x for x in result['summary'] if x['strategy_id']==identity)
            expected_mean=(first['net_return_proxy']+second['net_return_proxy'])/2
            expected_sd=abs(first['net_return_proxy']-second['net_return_proxy'])/math.sqrt(2)
            self.assertAlmostEqual(summary['mean_net_return_proxy'],expected_mean)
            self.assertAlmostEqual(summary['volatility_annualized_proxy'],expected_sd*math.sqrt(12))
            self.assertAlmostEqual(summary['sharpe_annualized_proxy'],expected_mean/expected_sd*math.sqrt(12),places=8)

    def test_held_missing_label_preserves_all_formation_weights_and_breaks_nav(self):
        b=hand_bundle(); before=self.run_checked(b,config(end_month='2026-08'))
        b['returns']=[r for r in b['returns'] if not (r['asset']=='A02' and r['date']=='2026-07-01')]
        after=self.run_checked(b,config(end_month='2026-08'))
        self.assertEqual(after['months'][0]['signals'],before['months'][0]['signals'])
        for identity in m.STRATEGIES:
            first=strategy(after,identity); second=strategy(after,identity,1)
            self.assertEqual(first['weights'],strategy(before,identity)['weights'])
            self.assertIsNone(first['gross_return'])
            self.assertEqual(first['estimated_cost'],.001)
            self.assertIsNotNone(second['net_return_proxy'])
            self.assertEqual(second['traded_weight'],0.)
            self.assertIsNone(second['nav_proxy'])
            metric=next(x for x in after['summary'] if x['strategy_id']==identity)
            self.assertEqual((metric['months_evaluated'],metric['gross_months'],metric['turnover_months']),(1,1,2))
            self.assertFalse(metric['cumulative_complete'])
            self.assertIsNone(metric['terminal_nav_proxy'])
            self.assertIsNone(metric['max_drawdown_proxy'])

    def test_nonheld_missing_label_does_not_change_portfolio(self):
        b=hand_bundle(); before=self.run_checked(b)
        next(r for r in b['returns'] if r['asset']=='A01' and r['date']=='2026-07-01')['value']=None
        after=self.run_checked(b)
        self.assertEqual(after['months'][0]['strategies'],before['months'][0]['strategies'])
        self.assertTrue(any(g['gross_return'] is None for g in after['months'][0]['groups']))

    def test_holding_requires_complete_even_with_available_signals(self):
        b=hand_bundle()
        next(r for r in b['returns'] if r['asset']=='A02' and r['date']=='2026-07-01')['value']=None
        result=self.run_checked(b,rule_set=rules(missing_policy='available',min_coverage=.5,max_missing_run=3))
        self.assertIsNone(strategy(result,'mom_id')['gross_return'])
        self.assertEqual(len(result['months'][0]['eligible_assets']),9)

    def test_target_calendar_incomplete_does_not_fake_complete_labels(self):
        b=hand_bundle(end='2026-07'); b['calendar']['end']='2026-07-15'
        result=self.run_checked(b)
        self.assertEqual(len(strategy(result,'momentum')['weights']),6)
        self.assertTrue(all('holding_calendar_incomplete' in x['reasons'] for x in result['months'][0]['labels']))
        self.assertIsNone(strategy(result,'momentum')['gross_return'])

    def test_ties_do_not_split_by_asset_name(self):
        b=hand_bundle()
        for r in b['returns']:
            if r['date'].startswith('2026-05'):
                r['value']=.1 if r['date'].endswith('01') else 0.
        result=self.run_checked(b)
        self.assertTrue(all(s['mom_group']==2 and s['id_group']==2 for s in result['months'][0]['signals']))
        self.assertEqual(result['status'],'not_evaluable')
        self.assertTrue(all(not s['weights'] for s in result['months'][0]['strategies']))
        self.assertEqual(m._terciles({'a':0,'b':0,'c':0,'d':1,'e':2,'f':3}),{'a':1,'b':1,'c':1,'d':2,'e':3,'f':3})

    def test_unformed_month_loses_cost_baseline_without_resetting_zero(self):
        b=hand_bundle(end='2026-10',target_labels=False)
        for r in b['returns']:
            if r['date'].startswith('2026-06'): r['value']=0.
        result=self.run_checked(b,config(end_month='2026-10'))
        for identity in m.STRATEGIES:
            gap=strategy(result,identity,1); resumed=strategy(result,identity,2); subsequent=strategy(result,identity,3)
            self.assertFalse(gap['weights'])
            self.assertIsNotNone(resumed['gross_return'])
            self.assertIsNone(resumed['estimated_cost'])
            self.assertIsNone(resumed['net_return_proxy'])
            self.assertIn('previous_target_unknown',resumed['reasons'])
            self.assertEqual(subsequent['traded_weight'],0.)
            self.assertIsNotNone(subsequent['net_return_proxy'])
            self.assertIsNone(subsequent['nav_proxy'])

    def test_future_changes_do_not_change_past_signals_weights_or_returns(self):
        b=hand_bundle(); before=self.run_checked(b,config(end_month='2026-08'))
        for row in b['returns']:
            if row['date'].startswith('2026-08'): row['value']=.4
        after=self.run_checked(b,config(end_month='2026-08'))
        self.assertEqual(before['months'][0],after['months'][0])
        self.assertEqual(before['months'][1]['signals'],after['months'][1]['signals'])
        self.assertEqual(strategy(before,'momentum',1)['weights'],strategy(after,'momentum',1)['weights'])
        self.assertNotEqual(before['input_digest'],after['input_digest'])

    def test_insufficient_joint_sample_does_not_relax_baseline_eligibility(self):
        b=hand_bundle()
        next(x for x in b['returns'] if x['asset']=='A00' and x['date']=='2026-05-01')['value']=None
        result=self.run_checked(b)
        self.assertEqual(len(result['months'][0]['eligible_assets']),8)
        for row in result['months'][0]['strategies']:
            self.assertEqual(row['weights'],[])
            self.assertIn('insufficient_eligible_assets',row['reasons'])

    def test_loss_below_minus_one_keeps_monthly_result_but_stops_nav(self):
        b=hand_bundle()
        for r in b['returns']:
            if r['date']=='2026-07-01' and r['asset'].endswith('0'): r['value']=10.
        result=self.run_checked(b,config(end_month='2026-08'))
        self.assertLess(strategy(result,'momentum')['net_return_proxy'],-1)
        self.assertIsNone(strategy(result,'momentum')['nav_proxy'])
        self.assertIsNone(strategy(result,'momentum',1)['nav_proxy'])
        self.assertIsNotNone(strategy(result,'momentum',1)['gross_return'])

    def test_zero_volatility_and_single_month_sharpe_null(self):
        result=self.run_checked(options=config(end_month='2026-08',cost_bps=0))
        for metric in result['summary']:
            self.assertEqual(metric['volatility_annualized_proxy'],0.)
            self.assertIsNone(metric['sharpe_annualized_proxy'])
        one=self.run_checked(options=config(cost_bps=0))
        self.assertTrue(all(x['sharpe_annualized_proxy'] is None for x in one['summary']))

    def test_paper_is_blocked_and_reference_not_supported(self):
        r=protocol.presets()[0]['config']; c=config(); b=m.demo_bundle(c,r)
        result=m.evaluate(r,c,b)
        self.assertEqual(result['status'],'blocked')
        self.assertTrue(all(x['net_return_proxy'] is None for x in result['months'][0]['strategies']))
        checked=reference.check(r,c,b,result)
        self.assertFalse(checked['supported'])
        self.assertIsNone(checked['passed'])
        self.assertEqual(checked['issues'],[])
        self.assertGreater(checked['checks'],100)

    def test_demo_actual_rules_24_months_and_independent_reference(self):
        c=config(start_month='2024-01',end_month='2025-12',min_assets=18)
        r=rules(mom_window_months=36,mom_skip_months=12,id_window_months=3,id_skip_months=0)
        b=m.demo_bundle(c,r)
        self.assertEqual(b['calendar']['start'],'2020-01-01')
        self.assertEqual(b['calendar']['end'],'2025-12-31')
        result=self.run_checked(b,c,r)
        self.assertEqual(len(result['months']),24)
        self.assertTrue(all(len(p['eligible_assets'])>=36 for p in result['months']))

    def test_order_and_inputs_remain_unchanged(self):
        b=hand_bundle(); before=deepcopy(b); result=self.run_checked(b)
        self.assertEqual(b,before)
        b['returns'].reverse(); b['assets'].reverse()
        shuffled=self.run_checked(b)
        self.assertEqual(result['months'],shuffled['months'])
        self.assertEqual(result['summary'],shuffled['summary'])

    def test_config_limits_and_closed_shape(self):
        cases=[config(end_month='2026-06'),config(end_month='2028-07'),config(start_month='2026-7'),
               config(cost_bps=True),config(cost_bps=float('inf')),config(cost_bps=-1),config(cost_bps=101),
               config(min_assets=True),config(min_assets=8),config(min_assets=129),config(schema_version=1.0),
               {**config(),'surprise':1}]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(m.MonthlyError): m.validate_config(case)

    def test_invalid_bundle_still_rejected(self):
        for modify in ('real','duplicate','nan'):
            b=hand_bundle()
            if modify=='real': b['data_kind']='real_market'
            if modify=='duplicate': b['returns'].append(dict(b['returns'][0]))
            if modify=='nan': b['returns'][0]['value']=float('nan')
            with self.subTest(modify=modify),self.assertRaises(m.MonthlyError): m.evaluate(rules(),config(),b)

if __name__=='__main__': unittest.main()
