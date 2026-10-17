"""Fixed-answer calibration of the critic, independent of actor sampling."""
from copy import deepcopy

from tools.skill_evaluation import digest, evaluate_case, valid_case_id


def validate_fixture_audit(fixtures, audit):
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError('empty fixtures')
    ids = [item['id'] for item in fixtures]
    if any(not valid_case_id(key) for key in ids) or len(set(key.casefold() for key in ids)) != len(ids):
        raise ValueError('invalid fixture identities')
    for item in fixtures:
        if item['expectedReviewStatus'] not in ('passed', 'failed'):
            raise ValueError('invalid expected review')
        if not isinstance(item['analysis'], dict) or not item['basis'].strip():
            raise ValueError('invalid analysis/basis')
        if any(v not in ('pass', 'fail', 'uncertain') for v in item['requiredCheckStatuses'].values()):
            raise ValueError('invalid check expectation')
    entries = audit.get('cases', [])
    if (audit.get('reviewer') != 'AI-independent' or audit.get('fixturesHash') != digest(fixtures)
            or len(entries) != len(ids) or {x['id'] for x in entries} != set(ids)
            or any(x.get('status') != 'accepted' or not x.get('reason', '').strip() for x in entries)):
        raise ValueError('fixture audit is missing, changed or incomplete')


def evaluate_fixed_answer(fixture, base_case, skills_root, provider):
    if fixture['baseCaseId'] != base_case['id']:
        raise ValueError('base case mismatch')
    # Only the original case and supplied answer enter the critic request.
    row = evaluate_case(base_case, skills_root, provider, fixed_analysis=fixture['analysis'])
    row['baseCaseId'] = base_case['id']
    row['id'] = fixture['id']
    row['fixedAnalysisHash'] = digest(fixture['analysis'])
    row['calibrationExpectation'] = {key: deepcopy(fixture[key]) for key in
                                    ('expectedReviewStatus', 'requiredCheckStatuses', 'basis', 'provenance')}
    if row['status'] not in ('passed', 'failed'):
        row['calibrationStatus'] = 'incomplete'
    else:
        actual = {item['id']: item['status'] for item in row['review']['checks']}
        matches = row['status'] == fixture['expectedReviewStatus'] and all(
            actual.get(key) == value for key, value in fixture['requiredCheckStatuses'].items())
        row['calibrationStatus'] = 'passed' if matches else 'failed'
    return row
