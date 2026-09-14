import unittest
from rail_dispatch import dispatch
class PublicTests(unittest.TestCase):
 def test_basic_arrive(self): self.assertEqual(dispatch([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T'}],{})['trains'][0]['status'],'active')
 def test_arrive_depart(self):
  o=dispatch([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T'},{'event_id':'d','kind':'DEPART','ts':2,'train_id':'T'}],{}); self.assertEqual(o['trains'][0]['status'],'departed')
 def test_cancel(self): self.assertFalse(dispatch([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T'},{'event_id':'c','kind':'CANCEL','ts':2,'train_id':'T'}],{})['trains'])
 def test_default_section(self): self.assertEqual(dispatch([],{})['section'],'A')
 def test_explicit_section(self): self.assertEqual(dispatch([],{'section':'X'})['section'],'X')
 def test_fifo_two(self): self.assertEqual(len(dispatch([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'A'},{'event_id':'b','kind':'ARRIVE','ts':2,'train_id':'B'}],{})['trains']),2)
 def test_unknown_depart(self): self.assertFalse(dispatch([{'event_id':'d','kind':'DEPART','ts':1,'train_id':'X'}],{})['trains'])
 def test_unknown_cancel(self): self.assertFalse(dispatch([{'event_id':'c','kind':'CANCEL','ts':1,'train_id':'X'}],{})['trains'])
 def test_payload_ignored(self): self.assertEqual(dispatch([{'event_id':'a','kind':'ARRIVE','ts':1,'train_id':'T','payload':{'priority':'emergency'}}],{})['trains'][0]['status'],'active')
 def test_invalid_kind(self):
  with self.assertRaises(ValueError): dispatch([{'event_id':'x','kind':'BOGUS','ts':0,'train_id':'T'}],{})
if __name__=='__main__': unittest.main()
