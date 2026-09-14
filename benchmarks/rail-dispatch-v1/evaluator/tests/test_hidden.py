import random, unittest, json
from rail_dispatch import dispatch

class HiddenSemantics(unittest.TestCase):
    def call(self, ev, cfg=None): return dispatch(ev, cfg or {'section':'A','capacity':2,'headway':2})
    def test_01_sorting(self):
        e=[{'event_id':'d','kind':'DEPART','ts':2,'train_id':'T'},{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T'}]; self.assertEqual(self.call(e)['trains'][0]['status'],'departed')
    def test_02_permutation(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T'},{'event_id':'d','kind':'DEPART','ts':3,'train_id':'T'}]; self.assertEqual(self.call(e),self.call(e[::-1]))
    def test_03_duplicate_idempotence(self):
        e={'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T'}; self.assertEqual(self.call([e,e]),self.call([e]))
    def test_04_conflict_error(self):
        a={'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T'}; b=dict(a,payload={'priority':'emergency'}); self.assertTrue(self.call([b,a])['errors'])
    def test_05_tie_event_id(self):
        e=[{'event_id':'b','kind':'ARRIVE','ts':1,'train_id':'B'},{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'}]; self.assertEqual(self.call(e),self.call(e[::-1]))
    def test_06_seq(self):
        e=[{'event_id':'d','kind':'DEPART','ts':1,'seq':2,'train_id':'T'},{'event_id':'a','kind':'ARRIVE','ts':1,'seq':1,'train_id':'T'}]; self.assertEqual(self.call(e)['trains'][0]['status'],'departed')
    def test_07_sections(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A','section':'A'},{'event_id':'b','kind':'ARRIVE','ts':1,'train_id':'B','section':'B'}]; self.assertEqual(len(self.call(e,{'capacity':1})['trains']),2)
    def test_08_priority(self):
        e=[{'event_id':'f','kind':'ARRIVE','ts':1,'train_id':'F','payload':{'priority':'freight'}},{'event_id':'e','kind':'ARRIVE','ts':2,'train_id':'E','payload':{'priority':'emergency'}}]; o=self.call(e,{'capacity':1}); self.assertEqual([x['status'] for x in o['trains'] if x['train_id']=='F'],['held'])
    def test_09_preempt_tie(self):
        e=[{'event_id':'p','kind':'ARRIVE','ts':1,'train_id':'P'},{'event_id':'q','kind':'ARRIVE','ts':2,'train_id':'Q'},{'event_id':'e','kind':'ARRIVE','ts':3,'train_id':'E','payload':{'priority':'emergency'}}]; o=self.call(e,{'capacity':2}); self.assertEqual([x for x in o['trains'] if x['status']=='held'][0]['train_id'],'P')
    def test_10_failure_releases_capacity(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'f','kind':'FAILURE','ts':2,'train_id':'A'},{'event_id':'b','kind':'ARRIVE','ts':3,'train_id':'B'}]; self.assertIn('B',[x['train_id'] for x in self.call(e,{'capacity':1})['trains']])
    def test_11_repair_held(self):
        blocked=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'e','kind':'ARRIVE','ts':2,'train_id':'E','payload':{'priority':'emergency'}},{'event_id':'r','kind':'REPAIR','ts':3,'train_id':'A'}]
        out=self.call(blocked,{'capacity':1}); by_id={t['train_id']:t for t in out['trains']}
        self.assertEqual(by_id['A']['status'],'held'); self.assertEqual(by_id['E']['status'],'active'); self.assertNotIn('r',out['accepted_event_ids']); self.assertTrue(out['safe'])
        released=blocked[:2]+[{'event_id':'d','kind':'DEPART','ts':3,'train_id':'E'},{'event_id':'r','kind':'REPAIR','ts':4,'train_id':'A'}]
        out=self.call(released,{'capacity':1}); by_id={t['train_id']:t for t in out['trains']}
        self.assertEqual(by_id['A']['status'],'active'); self.assertIn('r',out['accepted_event_ids']); self.assertTrue(out['safe'])
    def test_12_cancel(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'c','kind':'CANCEL','ts':2,'train_id':'A'}]; self.assertFalse(self.call(e)['trains'])
    def test_13_depart_only_active(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'f','kind':'FAILURE','ts':2,'train_id':'A'},{'event_id':'d','kind':'DEPART','ts':3,'train_id':'A'}]; self.assertEqual(self.call(e)['trains'][0]['status'],'failed')
    def test_14_capacity_count(self):
        e=[{'event_id':str(i),'kind':'ARRIVE','ts':i*10,'train_id':str(i)} for i in range(3)]; self.assertLessEqual(len([x for x in self.call(e,{'capacity':2,'headway':0})['trains'] if x['status']=='active']),2)
    def test_15_max_length(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A','payload':{'length':11}}]; self.assertFalse(self.call(e,{'max_length':10})['trains'])
    def test_16_headway(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'b','kind':'ARRIVE','ts':2,'train_id':'B'}]; self.assertEqual(len(self.call(e,{'capacity':3,'headway':5})['trains']),1)
    def test_17_headway_sections(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A','section':'A'},{'event_id':'b','kind':'ARRIVE','ts':2,'train_id':'B','section':'B'}]; self.assertEqual(len(self.call(e,{'capacity':2,'headway':5})['trains']),2)
    def test_18_safe(self): self.assertTrue(self.call([],{'capacity':0})['safe'])
    def test_19_json(self): json.dumps(self.call([]))
    def test_20_invalid_events(self):
        with self.assertRaises((TypeError,ValueError)): self.call(None)
    def test_21_invalid_kind(self):
        with self.assertRaises(ValueError): self.call([{'event_id':'x','kind':'NO','ts':1,'train_id':'T'}])
    def test_22_missing_ts(self):
        with self.assertRaises(ValueError): self.call([{'event_id':'x','kind':'ARRIVE','train_id':'T'}])
    def test_23_bad_config(self):
        with self.assertRaises((TypeError,ValueError)): self.call([],{'capacity':-1})
    def test_24_deterministic(self): self.assertEqual(self.call([]),self.call([]))
    def test_25_generated_shuffles(self):
        base=[{'event_id':str(i),'kind':'ARRIVE','ts':i*10,'train_id':str(i)} for i in range(3)]; ref=self.call(base,{'capacity':3,'headway':2})
        for s in range(20):
            x=list(base); random.Random(20240913+s).shuffle(x); x.append(dict(base[0])); self.assertEqual(ref,self.call(x,{'capacity':3,'headway':2}))
    def test_26_duplicate_payload_first_canonical(self):
        a={'event_id':'x','kind':'ARRIVE','ts':2,'train_id':'A','payload':{'priority':'freight'}}; b=dict(a,ts=1,payload={'priority':'emergency'}); self.assertEqual(self.call([a,b])['trains'][0]['priority'],'emergency')
    def test_27_unknown_section_independent(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A','section':'A'},{'event_id':'z','kind':'ARRIVE','ts':1,'train_id':'Z','section':'Z'}]; self.assertEqual(len(self.call(e,{'section':'A','capacity':1})['trains']),2)
    def test_28_output_keys(self): self.assertEqual(set(self.call([])),{'section','trains','accepted_event_ids','errors','safe'})
    def test_29_float_length(self): self.assertFalse(self.call([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A','payload':{'length':2.5}}],{'max_length':2})['trains'])
    def test_30_repeated_failure(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'f','kind':'FAILURE','ts':2,'train_id':'A'},{'event_id':'f2','kind':'FAILURE','ts':3,'train_id':'A'}]; self.assertEqual(self.call(e)['trains'][0]['status'],'failed')
    def test_31_unknown_cancel_not_accepted(self):
        self.assertNotIn('c',self.call([{'event_id':'c','kind':'CANCEL','ts':1,'train_id':'X'}])['accepted_event_ids'])
    def test_32_unknown_depart_not_accepted(self):
        self.assertNotIn('d',self.call([{'event_id':'d','kind':'DEPART','ts':1,'train_id':'X'}])['accepted_event_ids'])
    def test_33_failure_unknown_not_accepted(self):
        self.assertNotIn('f',self.call([{'event_id':'f','kind':'FAILURE','ts':1,'train_id':'X'}])['accepted_event_ids'])
    def test_34_repair_failed_invalid(self):
        e=[{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'f','kind':'FAILURE','ts':2,'train_id':'A'},{'event_id':'r','kind':'REPAIR','ts':3,'train_id':'A'}]; self.assertEqual(self.call(e)['trains'][0]['status'],'failed')
    def test_35_invalid_payload_length(self):
        with self.assertRaises((TypeError,ValueError)): self.call([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A','payload':{'length':'x'}}])
    def test_36_unknown_priority_rejected(self):
        with self.assertRaises((TypeError,ValueError)): self.call([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A','payload':{'priority':'vip'}}])
    def test_37_nan_ts_rejected(self):
        with self.assertRaises((TypeError,ValueError)): self.call([{'event_id':'a','kind':'ARRIVE','ts':float('nan'),'train_id':'A'}])
    def test_38_bool_ts_rejected(self):
        with self.assertRaises((TypeError,ValueError)): self.call([{'event_id':'a','kind':'ARRIVE','ts':True,'train_id':'A'}])
    def test_39_duplicate_event_conflict_error_sorted(self):
        a={'event_id':'x','kind':'ARRIVE','ts':1,'train_id':'A'}; b=dict(a,kind='CANCEL',ts=2); self.assertEqual(self.call([b,a])['errors'],['duplicate:x'])
    def test_40_capacity_zero(self):
        self.assertFalse(self.call([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'}],{'capacity':0})['trains'])
