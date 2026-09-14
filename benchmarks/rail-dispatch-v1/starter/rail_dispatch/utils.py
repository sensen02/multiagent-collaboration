"""Small standard-library helpers used by the starter implementation."""
import json

def canonical(value):
    return json.loads(json.dumps(value, sort_keys=True, separators=(',',':')))
def number(value, default=0):
    try:
        if isinstance(value,bool): return default
        return float(value)
    except (TypeError,ValueError): return default
def safe_id(value): return str(value)
def stable_train_sort(trains): return sorted(trains, key=lambda x:(x.get('section',''),x.get('train_id','')))
def clone_payload(payload): return canonical(payload or {})
def is_mapping(value): return isinstance(value,dict)
def nonnegative(value): return number(value,-1) >= 0
def describe_error(code, event_id=None): return code if event_id is None else code+':'+safe_id(event_id)
