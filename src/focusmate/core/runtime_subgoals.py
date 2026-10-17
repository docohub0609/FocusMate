"""Versioned working problems, separate from the immutable overall contract."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from jsonschema import Draft202012Validator
from focusmate.core.context_builder import hash_content

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 4000}
REF = {'type': 'string', 'minLength': 1, 'maxLength': 256}
REFS = {'type': 'array', 'items': REF, 'uniqueItems': True, 'maxItems': 24}
EVIDENCE = ['evaluatedUserExplanation', 'complexityCheck', 'codeStructureCheck',
            'compileResult', 'runResult', 'publicSampleRunResult', 'targetedRunResult']
PROPERTIES = {'kind': {'enum': ['create', 'revise', 'reuse', 'withdraw']}, 'key': REF,
    'expectedVersion': {'type': ['integer', 'null'], 'minimum': 1}, 'reason': TEXT,
    'objective': TEXT, 'checks': {'type': 'array', 'items': TEXT, 'minItems': 1, 'maxItems': 8},
    'evidenceRefs': dict(REFS, minItems=1), 'acceptableEvidence': {'type': 'array',
        'items': {'enum': EVIDENCE}, 'minItems': 1, 'uniqueItems': True},
    'dependsOn': REFS, 'relatedTo': REFS, 'replaces': REFS}
CHANGE_SCHEMA = {'type': 'object', 'properties': PROPERTIES,
    'required': ['kind', 'key', 'expectedVersion', 'reason'], 'additionalProperties': False,
    'allOf': [{'if': {'properties': {'kind': {'enum': ['create', 'revise']}}},
               'then': {'required': list(PROPERTIES)}}]}


def apply_changes(goals, changes, *, goal_id, available_refs, now=None):
    """Return a candidate aggregate; the caller commits it with the reviewed action."""
    if not isinstance(changes, list) or len(changes) > 12:
        raise ValueError('INVALID_GOAL_CHANGES')
    result, aliases = deepcopy(goals), {}
    now = now or datetime.now(timezone.utc).isoformat()
    seen = set()
    for change in changes:
        try: Draft202012Validator(CHANGE_SCHEMA).validate(change)
        except Exception as exc: raise ValueError('INVALID_GOAL_CHANGE') from exc
        key = change['key']
        if key in seen: raise ValueError('DUPLICATE_GOAL_CHANGE')
        seen.add(key)
        if change['kind'] == 'create':
            if not key.startswith('new:') or change['expectedVersion'] is not None:
                raise ValueError('INVALID_NEW_GOAL')
            aliases[key] = 'work-' + uuid4().hex
        else:
            item = result.get(key)
            if not item or item['goalId'] != goal_id: raise ValueError('GOAL_REFERENCE_MISMATCH')
            if item['versions'][-1]['version'] != change['expectedVersion']:
                raise ValueError('STALE_WORK_GOAL')
            if item['status'] in ('replaced', 'withdrawn'): raise ValueError('RETIRED_WORK_GOAL')
    for change in changes:
        key = aliases.get(change['key'], change['key'])
        kind = change['kind']
        if kind == 'reuse': continue
        if kind == 'withdraw':
            result[key].update(status='withdrawn', retiredReason=change['reason'], retiredAt=now)
            continue
        if not set(change['evidenceRefs']) <= set(available_refs):
            raise ValueError('GOAL_EVIDENCE_REFERENCE_MISMATCH')
        if any(not text.strip() for text in [change['objective'], *change['checks']]):
            raise ValueError('EMPTY_WORK_REQUIREMENT')
        record = result.setdefault(key, {'subgoalId': key, 'goalId': goal_id,
            'status': 'active', 'versions': [], 'verificationRefs': []})
        version = {name: deepcopy(change[name]) for name in
                   ('objective', 'checks', 'acceptableEvidence', 'evidenceRefs', 'reason')}
        version.update(version=len(record['versions']) + 1, createdAt=now)
        for name in ('dependsOn', 'relatedTo', 'replaces'):
            version[name] = [aliases.get(ref, ref) for ref in change[name]]
            if key in version[name]: raise ValueError('SELF_GOAL_REFERENCE')
        version['contentHash'] = hash_content(version)
        record['versions'].append(version)
        record['status'] = 'active'
    for key, record in result.items():
        current = record['versions'][-1]
        for name in ('dependsOn', 'relatedTo', 'replaces'):
            for ref in current[name]:
                if ref not in result or result[ref]['goalId'] != goal_id:
                    raise ValueError('GOAL_REFERENCE_MISMATCH')
        if key in aliases.values() or any(c['key']==key and c['kind']=='revise' for c in changes):
            for ref in current['replaces']:
                if result[ref].get('replacedBy')==key:continue
                if result[ref]['status'] in ('replaced', 'withdrawn'):
                    raise ValueError('RETIRED_REPLACEMENT_TARGET')
                result[ref].update(status='replaced', replacedBy=key, retiredAt=now)
    active = {key for key, item in result.items() if item['status'] not in ('withdrawn', 'replaced')}
    for key in active:
        if not set(result[key]['versions'][-1]['dependsOn']) <= active:
            raise ValueError('RETIRED_DEPENDENCY')
    def visit(key, stack, done):
        if key in stack: raise ValueError('GOAL_DEPENDENCY_CYCLE')
        if key in done: return
        for ref in result[key]['versions'][-1]['dependsOn']: visit(ref, stack | {key}, done)
        done.add(key)
    done = set()
    for key in active: visit(key, set(), done)
    if len(active) > 24: raise ValueError('TOO_MANY_ACTIVE_GOALS')
    return result, aliases


def current_goals(goals):
    return [{**deepcopy(item['versions'][-1]), 'subgoalId': key, 'goalId': item['goalId'],
             'status': item['status']} for key, item in goals.items()
            if item['status'] not in ('replaced', 'withdrawn')]
