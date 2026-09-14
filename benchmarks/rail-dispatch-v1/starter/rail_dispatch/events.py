"""Event normalization. Deliberate bug: conflict handling is input-order based."""
VALID = {'ARRIVE','DEPART','CANCEL','FAILURE','REPAIR'}
REQUIRED = ('event_id','kind','ts','train_id')
def validate(event):
    if not isinstance(event, dict): raise TypeError('event must be object')
    missing = [k for k in REQUIRED if k not in event]
    if missing: raise ValueError('missing '+missing[0])
    if event['kind'] not in VALID: raise ValueError('unknown kind')
    if not isinstance(event['ts'], (int,float)) or isinstance(event['ts'], bool): raise TypeError('ts must be numeric')
    return event
def order_key(e): return (e['ts'], e.get('seq', 0), str(e['event_id']))
def normalize(events):
    if not isinstance(events, list): raise TypeError('events must be list')
    for e in events: validate(e)
    chosen, conflicts = {}, []
    for e in sorted(events, key=order_key):
        i = e['event_id']
        if i in chosen:
            if e != chosen[i]: conflicts.append('duplicate:'+str(i))
            continue
        chosen[i] = e
    return list(chosen.values()), conflicts
