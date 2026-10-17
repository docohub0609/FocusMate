"""Blind review adapter: preserve original binding, admit fixed dialogue references."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from tools.skill_evaluation import digest
from tools.skill_hint_review import validate_review, compare_checks


def validate(packet, review):
    if review.get('packetHash') != digest(packet) or review.get('experimentHash') != packet['experimentHash']:
        raise ValueError('review does not bind original packet')
    # Existing protocol validates citation membership in materials. Add only the
    # already frozen dialogue as evidence to a private copy, never to actor input.
    expanded = deepcopy(packet)
    for item in expanded['items']:
        existing = {p['ref'] for p in item['materials']['problemFacts']}
        for turn in item['dialogue']:
            if turn['ref'] in existing:
                raise ValueError('duplicate dialogue reference')
            item['materials']['problemFacts'].append({'ref': turn['ref'], 'text': turn['text']})
            existing.add(turn['ref'])
    bound = deepcopy(review)
    bound['packetHash'] = digest(expanded)
    return validate_review(expanded, bound)


def compare(directory, review_paths):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    packet = json.loads((directory / 'blind-packet.json').read_text(encoding='utf-8'))
    unhashed = {k: v for k, v in manifest.items() if k != 'experimentHash'}
    if digest(unhashed) != manifest['experimentHash'] or packet['experimentHash'] != manifest['experimentHash']:
        raise ValueError('manifest or packet changed')
    packet_by_id = {item['answerId']: item for item in packet['items']}
    if len(packet_by_id) != len(packet['items']) or set(packet_by_id) != {e['answerId'] for e in manifest['schedule']}:
        raise ValueError('packet/schedule coverage mismatch')
    frozen_cases = {c['case']['id']: c for c in manifest['cases']}
    for entry in manifest['schedule']:
        record = json.loads((directory / (entry['recordId'] + '.json')).read_text(encoding='utf-8'))
        if any(record[k] != entry[k] for k in entry) or record['experimentHash'] != manifest['experimentHash']:
            raise ValueError('record identity mismatch')
        item = packet_by_id[entry['answerId']]
        if (item['hint'] != record['hint'] or item['evidenceRefs'] != record['evidenceRefs']
                or item['answerHash'] != digest({'hint': record['hint'], 'evidenceRefs': record['evidenceRefs']})):
            raise ValueError('record answer changed')
        payload = frozen_cases[entry['caseId']]['payload']
        if any(item[k] != payload[k] for k in ('materials', 'hintLevel', 'dialogue', 'runEvidenceVersions')):
            raise ValueError('blind materials changed')
    judgments = {}
    for path in review_paths:
        review = json.loads(Path(path).read_text(encoding='utf-8'))
        ids = {row['answerId'] for row in review['answers']}
        subset = {**packet, 'items': [item for item in packet['items'] if item['answerId'] in ids]}
        validated = validate(subset, review)
        for identity, judgment in validated.items():
            if identity in judgments:
                raise ValueError('duplicate review; resolve disputes explicitly')
            judgments[identity] = judgment
    expected = {item['answerId'] for item in packet['items'] if item['hint'] is not None}
    if set(judgments) != expected:
        raise ValueError('review coverage incomplete')
    developer_path = directory / 'developer-review.json'
    developer = json.loads(developer_path.read_text(encoding='utf-8')) if developer_path.exists() else None
    developer_complete = (isinstance(developer, dict)
                          and developer.get('experimentHash') == manifest['experimentHash']
                          and developer.get('reviewer') == 'developer-AI'
                          and developer.get('humanReviewed') is False
                          and set(developer.get('reviewedAnswerIds', [])) == expected)
    unresolved = set(developer.get('unresolvedAnswerIds', [])) if developer_complete else set()
    if not unresolved <= expected:
        raise ValueError('unknown developer dispute')
    mapping = {(e['caseId'], e['round'], e['arm']): e['answerId'] for e in manifest['schedule']}
    comparisons = []
    for case in manifest['cases']:
        identity = case['case']['id']
        for number in (1, 2):
            def checks(arm):
                answer_id = mapping[identity, number, arm]
                return None if answer_id in unresolved else judgments.get(answer_id, {}).get('checks')
            comparisons.append({'caseId': identity, 'round': number,
                                'fullVsOrdinary': compare_checks(checks('ordinary'), checks('full')),
                                'selectiveVsOrdinary': compare_checks(checks('ordinary'), checks('selective')),
                                'selectiveVsFull': compare_checks(checks('full'), checks('selective'))})
    stable = [c['case']['id'] for c in manifest['cases']
              if all(r['selectiveVsOrdinary'] == 'improvement' for r in comparisons if r['caseId'] == c['case']['id'])]
    regression = any(r['selectiveVsOrdinary'] == 'regression' for r in comparisons)
    held_out = bool(manifest['cases']) and all(c['case']['phase'] == 'holdout' for c in manifest['cases'])
    return {'experimentHash': manifest['experimentHash'], 'humanReviewed': False,
            'reviewKind': 'independent-AI', 'comparisons': comparisons,
            'counts': {key: dict(Counter(r[key] for r in comparisons)) for key in
                       ('fullVsOrdinary', 'selectiveVsOrdinary', 'selectiveVsFull')},
            'stableSelectiveImprovementCases': stable,
            'developerReviewComplete': developer_complete, 'heldOutPhase': held_out,
            'unresolvedAnswerIds': sorted(unresolved),
            'meetsExpansionGate': held_out and developer_complete and len(stable) >= 2 and not regression,
            'interpretation': 'Gate applies only to held-out phase after developer factual review; no statistical or universal proof.'}
