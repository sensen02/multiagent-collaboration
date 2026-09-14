from .events import validate
from .model import Train
from .state import State

def dispatch(events, config):
    if not isinstance(events,list): raise TypeError('events must be list')
    if not isinstance(config,dict): raise TypeError('config must be dict')
    state=State(config); section=config.get('section','A'); bucket=state.section(section)
    for e in events:
        validate(e); tid=str(e['train_id']); kind=e['kind']
        if e.get('section',section)!=section: continue
        if kind=='ARRIVE':
            if tid not in bucket: bucket[tid]=Train(tid,section=section,last_arrive=e['ts']); state.accepted.append(e['event_id'])
        elif kind=='DEPART' and tid in bucket and bucket[tid].status=='active': bucket[tid].status='departed'; state.accepted.append(e['event_id'])
        elif kind=='CANCEL': bucket.pop(tid,None); state.accepted.append(e['event_id'])
    out=state.output(); out['section']=section; return out
