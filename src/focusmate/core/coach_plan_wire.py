"""A shallow model response, normalized into the unchanged internal plan contract."""
from copy import deepcopy
from jsonschema import Draft202012Validator
from focusmate.core.dynamic_coach_roles import PLAN_SCHEMA


def _model_internal_schema(internal_schema=None):
    schema=deepcopy(internal_schema or PLAN_SCHEMA)
    follow=schema['properties']['action']['properties']['followUp']
    follow['allOf']=[{
        'if':{'properties':{'kind':{'const':'awaitEvidence'}}},
        'then':{'properties':{'criterionIds':{'minItems':1},'checks':{'minItems':1}}},
        'else':{'properties':{'criterionIds':{'maxItems':0},'checks':{'maxItems':0}}},
    }]
    follow['description']='awaitInformation/none: criterionIds and checks must both be []; awaitEvidence: both must be nonempty. Requesting clarification is not verification.'
    return schema


def model_plan_schema(internal_schema=None):
    schema=_model_internal_schema(internal_schema)
    action=schema['properties'].pop('action')
    schema['required'].remove('action')
    schema['properties'].update(action['properties'])
    schema['required'].extend(action['required'])
    return schema


def normalize_plan(value):
    # Old replies and persisted decisions stay readable; never discard unknown keys.
    if isinstance(value,dict) and 'action' in value:
        Draft202012Validator(_model_internal_schema()).validate(value)
        return deepcopy(value)
    Draft202012Validator(model_plan_schema()).validate(value)
    controls=('goalChanges','next','checkTargets')
    return {**{k:deepcopy(value[k]) for k in controls},
            'action':{k:deepcopy(v) for k,v in value.items() if k not in controls}}
