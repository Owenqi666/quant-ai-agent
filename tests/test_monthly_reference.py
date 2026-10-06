from copy import deepcopy
import ast
import math
from pathlib import Path
import unittest
from paper_alpha import monthly_evaluation as core, monthly_reference as oracle, research_protocol
from tests.test_monthly_evaluation import rules, config, hand_bundle


class MonthlyReferenceTests(unittest.TestCase):
    def test_oracle_does_not_import_production_calculators(self):
        tree=ast.parse(Path(oracle.__file__).read_text())
        imports=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): imports.extend(n.name for n in node.names)
            elif isinstance(node,ast.ImportFrom): imports.append(node.module or '')
        self.assertFalse(any('monthly_evaluation' in name or 'research_protocol' in name for name in imports))

    def test_legal_window_policy_and_calendar_year_edges(self):
        cases=[
            (rules(mom_skip_months=0,id_skip_months=0),config(start_month='1905-01',end_month='1905-02',cost_bps=0,min_assets=9)),
            (rules(mom_window_months=36,id_window_months=36,mom_skip_months=12,id_skip_months=12),config(start_month='2099-11',end_month='2099-12',cost_bps=100,min_assets=128)),
            (rules(mom_window_months=6,id_window_months=11,mom_skip_months=0,id_skip_months=2),config(start_month='2024-02',end_month='2024-04')),
            (rules(missing_policy='available',min_coverage=.5,max_missing_run=366),config()),
            (rules(missing_policy='available',min_coverage=1,max_missing_run=0),config()),
        ]
        for r,c in cases:
            with self.subTest(r=r,c=c):
                b=core.demo_bundle(c,r); value=core.evaluate(r,c,b)
                check=oracle.check(r,c,b,value)
                self.assertTrue(check['passed'],check)
                self.assertGreater(check['checks'],100)

    def test_each_scientific_layer_and_metadata_are_checked(self):
        r,c,b=rules(),config(),hand_bundle()
        result=core.evaluate(r,c,b)
        paths=[('source_id',),('semantics_version',),('config_digest',),('input_digest',),('warnings',0),
               ('months',0,'windows','momentum','start'),('months',0,'signals',0,'id'),
               ('months',0,'signals',0,'mom_group'),('months',0,'groups',0,'gross_return'),
               ('months',0,'labels',0,'return_value'),('months',0,'labels',0,'coverage','zero'),
               ('months',0,'strategies',0,'weights',0,'weight'),('months',0,'strategies',0,'estimated_cost'),
               ('months',0,'strategies',0,'net_return_proxy'),('months',0,'strategies',0,'nav_proxy'),
               ('months',0,'difference_net_proxy'),('summary',0,'months_evaluated'),('summary',0,'mean_gross_return')]
        for path in paths:
            altered=deepcopy(result); target=altered
            for key in path[:-1]: target=target[key]
            key=path[-1]; target[key]=target[key]+1 if type(target[key]) in (float,int) else 'tampered'
            with self.subTest(path=path): self.assertFalse(oracle.check(r,c,b,altered)['passed'])
        for unexpected in ('months','top'):
            altered=deepcopy(result)
            (altered['months'][0] if unexpected=='months' else altered)['extra']='surprise'
            self.assertFalse(oracle.check(r,c,b,altered)['passed'])

    def test_paper_blocked_numbers_and_identity_are_not_trusted(self):
        r=research_protocol.presets()[0]['config']; c=config(end_month='2026-08')
        b=core.demo_bundle(c,r); result=core.evaluate(r,c,b)
        check=oracle.check(r,c,b,result)
        self.assertEqual((check['supported'],check['passed'],check['issues']),(False,None,[]))
        for path in [('summary',0,'mean_gross_return'),('months',0,'strategies',0,'net_return_proxy'),('status',),('warnings',0)]:
            altered=deepcopy(result); target=altered
            for key in path[:-1]: target=target[key]
            target[path[-1]]=1. if path[-1] not in ('status',0) else 'evaluated'
            check=oracle.check(r,c,b,altered)
            self.assertFalse(check['supported']); self.assertIsNone(check['passed']); self.assertTrue(check['issues'])

    def test_invalid_or_unbounded_inputs_fail_without_computation(self):
        r,c,b=rules(),config(),hand_bundle(); value=core.evaluate(r,c,b)
        bad=[]
        changed=deepcopy(b); changed['returns'].append(deepcopy(changed['returns'][0])); bad.append((r,c,changed))
        changed=deepcopy(b); changed['calendar']['sessions'].reverse(); bad.append((r,c,changed))
        changed=deepcopy(b); changed['returns'][0]['value']=math.nan; bad.append((r,c,changed))
        changed=deepcopy(b); changed['returns'][0]['value']=True; bad.append((r,c,changed))
        bad += [(r,{**c,'end_month':'9999-12'},b),(r,{**c,'end_month':'2029-01'},b),
                ({**r,'id_window_months':100000000},c,b),([],c,b),(r,c,[])]
        for args in bad:
            with self.subTest(args=str(args)[:80]):
                checked=oracle.check(*args,value)
                self.assertFalse(checked['passed']); self.assertEqual(checked['checks'],0)

    def test_compounding_sign_extremes_and_complete_loss(self):
        r,c,b=rules(),config(),hand_bundle()
        for values in ((1e-18,0.,0.),(1.,-.5,0.),(-1.,1e300,0.),(1e300,1e300,0.)):
            changed=deepcopy(b)
            for row in changed['returns']:
                if row['date'][:7]=='2026-05' and row['asset']=='A00': row['value']=values[int(row['date'][-2:])-1]
            checked=oracle.check(r,c,changed,core.evaluate(r,c,changed))
            self.assertTrue(checked['passed'],checked)


if __name__=='__main__': unittest.main()
