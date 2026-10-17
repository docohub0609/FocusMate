"""Omit server-owned fields on the wire; restore only from the pinned request."""
from copy import deepcopy
import json


def omit_bound_fields(schema, bindings):
    result=deepcopy(schema)
    for key in bindings:
        result['properties'].pop(key,None)
        if key in result.get('required',[]):result['required'].remove(key)
    return result


def restore_bound_fields(value, bindings):
    if not isinstance(value,dict):raise ValueError('MODEL_OBJECT_REQUIRED')
    result=deepcopy(value)
    for key,expected in bindings.items():
        # Legacy replies may still echo fields, but a mismatch is never repaired.
        if key in result and json.dumps(result[key],sort_keys=True,ensure_ascii=False)!=json.dumps(expected,sort_keys=True,ensure_ascii=False):
            raise ValueError('MODEL_BINDING_MISMATCH:'+key)
        result[key]=deepcopy(expected)
    return result
