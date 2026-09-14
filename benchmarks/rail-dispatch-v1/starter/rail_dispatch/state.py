from .model import Train, priority_of
class State:
    def __init__(self, config):
        self.config=config; self.sections={}; self.accepted=[]; self.errors=[]
    def section(self, name): return self.sections.setdefault(name, {})
    def active(self, sec): return [t for t in self.section(sec).values() if t.status == 'active']
    def occupied(self, sec): return [t for t in self.section(sec).values() if t.status in ('active','held')]
    def add(self, e, sec):
        p=e.get('payload') or {}; t=Train(str(e['train_id']), priority_of(p.get('priority')), p.get('length',1), 'active', sec, e['ts'])
        self.section(sec)[t.train_id]=t; return t
    def output(self):
        all_t=[]
        for sec in sorted(self.sections): all_t.extend(t.json() for t in self.sections[sec].values())
        all_t.sort(key=lambda x:(x['section'],x['train_id']))
        return {'trains':all_t,'accepted_event_ids':list(self.accepted),'errors':sorted(self.errors),'safe':self.safe()}
    def safe(self):
        cap=self.config.get('capacity',1)
        return all(len(self.occupied(s)) <= cap for s in self.sections)
