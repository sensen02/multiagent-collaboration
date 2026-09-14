from .model import PRIORITY
def section_for(e, config): return e.get('section', config.get('section','A'))
def can_arrive(state, train, sec, ts):
    cfg=state.config; cap=cfg.get('capacity',1)
    if len(state.active(sec)) < cap: return True
    if train.priority != 'emergency': return False
    candidates=sorted(state.active(sec), key=lambda t:(PRIORITY.get(t.priority,1),t.train_id))
    if not candidates or PRIORITY[train.priority] <= PRIORITY.get(candidates[0].priority,1): return False
    candidates[0].status='held'; return True
def valid_length(train, config): return train.length <= config.get('max_length', float('inf'))
def valid_headway(state, sec, ts):
    h=state.config.get('headway',0)
    return all(abs(ts-t.last_arrive) >= h for t in state.active(sec))
