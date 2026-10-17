"""Validate independent blind reviews; compare evidence-backed dimensions, not prose keywords."""
from tools.skill_evaluation import catalog, digest

CHECKS = ('accuracy', 'focus', 'helpfulness', 'coachingScope')
LEVELS = {'none': 0, 'partial': 1, 'sufficient': 2}


def validate_review(packet, review):
    """A review is an AI opinion bound to an exact visible answer, never a human sign-off."""
    if (set(review) != {'reviewer', 'humanReviewed', 'experimentHash', 'packetHash', 'answers'}
            or review['reviewer'] != 'AI-independent' or review['humanReviewed'] is not False
            or review['experimentHash'] != packet['experimentHash']
            or review['packetHash'] != digest(packet)):
        raise ValueError('invalid review identity or experiment')
    items = {item['answerId']: item for item in packet['items']}
    if len(items) != len(packet['items']):
        raise ValueError('duplicate blind answer')
    expected = {key for key, item in items.items() if item['hint'] is not None}
    indexed = {}
    for row in review['answers']:
        key = row['answerId']
        if key not in expected or key in indexed or set(row) != {'answerId', 'answerHash', 'checks'}:
            raise ValueError('unknown or duplicate reviewed answer')
        item = items[key]
        actual = digest({'hint': item['hint'], 'evidenceRefs': item['evidenceRefs']})
        if row['answerHash'] != item['answerHash'] or row['answerHash'] != actual:
            raise ValueError('answer changed after review')
        if set(row['checks']) != set(CHECKS):
            raise ValueError('incomplete review checks')
        refs = catalog(item['materials'])
        for name, check in row['checks'].items():
            required = {'status', 'reason', 'quote', 'evidenceRefs'}
            if name == 'helpfulness': required.add('level')
            if set(check) != required or check['status'] not in ('pass', 'fail', 'uncertain'):
                raise ValueError('invalid review check')
            if not isinstance(check['reason'], str) or not check['reason'].strip():
                raise ValueError('missing review reason')
            quote = check['quote']
            if not isinstance(quote, str) or (quote and quote not in item['hint']):
                raise ValueError('quote not in visible answer')
            evidence = check['evidenceRefs']
            if (not isinstance(evidence, list) or not all(isinstance(ref, str) for ref in evidence)
                    or len(evidence) != len(set(evidence)) or not set(evidence) <= refs):
                raise ValueError('invalid review references')
            if check['status'] != 'uncertain' and (not quote.strip() or not evidence):
                raise ValueError('judgment without traceable evidence')
            if name == 'helpfulness':
                if check['level'] not in (*LEVELS, 'uncertain'):
                    raise ValueError('invalid helpfulness level')
                if ((check['status'] == 'uncertain') != (check['level'] == 'uncertain')
                        or (check['status'] == 'pass') != (check['level'] == 'sufficient')):
                    raise ValueError('inconsistent helpfulness judgment')
        indexed[key] = row
    if set(indexed) != expected:
        raise ValueError('review coverage incomplete')
    return indexed


def compare_checks(baseline, skill):
    """Prefer only an unambiguous improvement with no worse dimension; exclude tradeoffs."""
    if baseline is None or skill is None:
        return 'insufficientEvidence'
    if any(check['status'] == 'uncertain' for checks in (baseline, skill) for check in checks.values()):
        return 'insufficientEvidence'
    def vector(checks):
        return [LEVELS[checks[name]['level']] if name == 'helpfulness'
                else 2 if checks[name]['status'] == 'pass' else 0 for name in CHECKS]
    a, b = vector(baseline), vector(skill)
    if a == b: return 'tie'
    if all(y >= x for x, y in zip(a, b)): return 'improvement'
    if all(y <= x for x, y in zip(a, b)): return 'regression'
    return 'insufficientEvidence'
