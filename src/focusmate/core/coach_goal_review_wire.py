"""Require explicit model review of every associated working-goal condition.

This is a wire extension, not a migration of saved reviews. The program checks
coverage and contradictory verdicts; it does not classify condition semantics.
"""
from copy import deepcopy

from jsonschema import Draft202012Validator

from focusmate.core.context_builder import hash_content


REQUIREMENT_REVIEW = {
    'type': 'object',
    'properties': {
        'requirementId': {'type': 'string', 'minLength': 1, 'maxLength': 256},
        'verdict': {'enum': ['pass', 'fail', 'uncertain']},
        'reason': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
    },
    'required': ['requirementId', 'verdict', 'reason'],
    'additionalProperties': False,
}


def goal_requirement_catalog(s, r):
    """Pin conditions from the prepared candidate, including proposed revisions.

    Repeated condition text still has separate ordinals. Goals selected by the
    action, checks or non-withdrawal changes are reviewed; unrelated ones are not.
    """
    prepared = r['prepared']
    candidate = prepared['candidate']
    aliases = prepared.get('aliases', {})
    changed = [aliases.get(change['key'], change['key'])
               for change in candidate.get('goalChanges', [])
               if change['kind'] in ('create', 'revise', 'reuse')]
    targets = list(dict.fromkeys(
        candidate['action']['subgoalIds'] + candidate['checkTargets'] + changed))
    # V2 still validates every association below, but previously accepted
    # conditions are not re-litigated unless their meaning is actually revised.
    changed_conditions = {aliases.get(change['key'], change['key'])
        for change in candidate.get('goalChanges', []) if change['kind'] in ('create', 'revise')}
    catalog = []
    for key in targets:
        goal = prepared['goals'].get(key)
        if (not goal or goal.get('goalId') != s['goalContext']['goalId']
                or goal.get('subgoalId') != key
                or goal.get('status') in ('withdrawn', 'replaced')
                or not goal.get('versions')):
            raise ValueError('GOAL_REVIEW_TARGET_INVALID')
        version = goal['versions'][-1]
        if (type(version.get('version')) is not int or version['version'] < 1
                or not isinstance(version.get('checks'), list) or not version['checks']):
            raise ValueError('GOAL_REVIEW_TARGET_INVALID')
        if r.get('workflowVersion', 1) >= 2 and key not in changed_conditions:
            continue
        for ordinal, requirement in enumerate(version['checks']):
            if not isinstance(requirement, str) or not requirement.strip():
                raise ValueError('GOAL_REVIEW_TARGET_INVALID')
            pin = {'goalId': goal['goalId'], 'subgoalId': key,
                   'workGoalVersion': version['version'], 'ordinal': ordinal,
                   'requirement': requirement}
            catalog.append({**pin, 'requirementId':
                            'requirement-' + hash_content(pin).split(':', 1)[1]})
    return catalog


def _requirement_ids(catalog):
    ids = [item['requirementId'] for item in catalog]
    if (any(not isinstance(key, str) or not key for key in ids)
            or len(ids) != len(set(ids))):
        raise ValueError('INVALID_GOAL_REVIEW_CATALOG')
    return ids


def goal_review_wire_schema(base_schema, catalog):
    """Extend a detached schema while leaving legacy persistence unchanged."""
    ids = _requirement_ids(catalog)
    schema = deepcopy(base_schema)
    goal_schema = schema['properties']['goalReview']
    item = deepcopy(REQUIREMENT_REVIEW)
    if ids:
        item['properties']['requirementId']['enum'] = ids
    goal_schema['properties']['requirementReviews'] = {
        'type': 'array', 'items': item, 'minItems': len(ids), 'maxItems': len(ids),
    }
    if 'requirementReviews' not in goal_schema['required']:
        goal_schema['required'].append('requirementReviews')
    return schema


def validate_goal_review_wire(review, catalog):
    """Return (legacy review, detached coverage audit), or reject the new reply.

    The caller must still apply the existing action/goal review validators to
    the returned review. Old persisted reviews do not enter this wire check.
    """
    expected = set(_requirement_ids(catalog))
    if not isinstance(review, dict):
        raise ValueError('GOAL_REVIEW_COVERAGE_REQUIRED')
    goal_review = review.get('goalReview')
    if not isinstance(goal_review, dict) or 'requirementReviews' not in goal_review:
        raise ValueError('GOAL_REVIEW_COVERAGE_REQUIRED')
    checks = goal_review['requirementReviews']
    if not isinstance(checks, list):
        raise ValueError('INVALID_GOAL_REQUIREMENT_REVIEW')
    # Validate at the original reply path so the Executor can retain the exact
    # item/field and jsonschema message for repair, without rewriting the reply.
    Draft202012Validator({'type': 'object', 'properties': {
        'goalReview': {'type': 'object', 'properties': {
            'requirementReviews': {'type': 'array', 'items': REQUIREMENT_REVIEW},
        }},
    }}).validate(review)
    for check in checks:
        if not check['reason'].strip():
            raise ValueError('INVALID_GOAL_REQUIREMENT_REVIEW')
    actual = [check['requirementId'] for check in checks]
    if len(actual) != len(set(actual)):
        raise ValueError('DUPLICATE_GOAL_REVIEW_REQUIREMENT')
    if set(actual) - expected:
        raise ValueError('UNKNOWN_GOAL_REVIEW_REQUIREMENT')
    if set(actual) != expected:
        raise ValueError('INCOMPLETE_GOAL_REVIEW_COVERAGE')
    if goal_review.get('status') not in ('passed', 'failed', 'inconclusive'):
        raise ValueError('INVALID_GOAL_REVIEW_STATUS')
    if goal_review['status'] == 'passed' and any(check['verdict'] != 'pass' for check in checks):
        raise ValueError('GOAL_REVIEW_VERDICT_CONFLICT')
    legacy = deepcopy(review)
    legacy['goalReview'].pop('requirementReviews')
    audit = {'schemaVersion': '1.0.0', 'requirements': deepcopy(catalog),
             'requirementReviews': deepcopy(checks), 'overallStatus': goal_review['status']}
    return legacy, audit
