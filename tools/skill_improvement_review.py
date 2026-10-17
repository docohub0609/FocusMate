"""Bind blind opinions to frozen T054 answers; report benefit without equating use with success."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from tools.skill_evaluation import digest
from tools.skill_routing_review import validate
from tools.skill_hint_review import compare_checks


def read_reviews(directory, paths):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    packet = json.loads((directory / 'blind-packet.json').read_text(encoding='utf-8'))
    if digest({k: v for k, v in manifest.items() if k != 'experimentHash'}) != manifest['experimentHash']:
        raise ValueError('MANIFEST_CHANGED')
    if packet['experimentHash'] != manifest['experimentHash'] or packet['rubric'] != manifest['rubric']:
        raise ValueError('BLIND_PACKET_CHANGED')
    by_id = {i['answerId']: i for i in packet['items']}
    if len(by_id) != len(packet['items']) or set(by_id) != {e['answerId'] for e in manifest['schedule']}:
        raise ValueError('BLIND_COVERAGE_CHANGED')
    cases = {c['case']['id']: c for c in manifest['cases']}
    rows = {}
    for e in manifest['schedule']:
        row = json.loads((directory / (e['recordId'] + '.json')).read_text(encoding='utf-8'))
        if row['experimentHash'] != manifest['experimentHash'] or any(row[k] != e[k] for k in e):
            raise ValueError('RECORD_IDENTITY_CHANGED')
        item = by_id[e['answerId']]
        for k in ('materials', 'dialogue', 'hintLevel', 'runEvidenceVersions'):
            if item[k] != cases[e['caseId']]['payload'][k]:
                raise ValueError('BLIND_MATERIAL_CHANGED')
        answer = {'hint': row['hint'], 'evidenceRefs': row['evidenceRefs']}
        if any(item[k] != v for k, v in answer.items()) or digest(answer) != item['answerHash']:
            raise ValueError('ANSWER_CHANGED')
        if (row.get('status') not in ('generated', 'invalid', 'unavailable')
                or (row['status'] == 'generated') != (isinstance(row['hint'], str) and bool(row['hint'].strip()))
                or row['status'] != 'generated' and (row['hint'] is not None or row['evidenceRefs'])):
            raise ValueError('RESULT_STATUS_CHANGED')
        if row['status'] == 'generated':
            actual = json.loads(row['calls'][-1]['response']['content'])
            if any(actual.get(k) != v for k, v in answer.items()):
                raise ValueError('VISIBLE_RESPONSE_CHANGED')
            if row.get('loaded') != row['calls'][-1]['request']['payload'].get('skill'):
                raise ValueError('LOADED_CONTENT_CHANGED')
            if e['arm'].startswith('selective'):
                selection = json.loads(row['calls'][0]['response']['content'])
                if row.get('selection') != selection:
                    raise ValueError('SELECTION_CHANGED')
                if selection.get('action') == 'directHint':
                    if row.get('loaded') is not None or len(row['calls']) != 1:
                        raise ValueError('DIRECT_HINT_CANNOT_CLAIM_READING')
                elif selection.get('action') == 'useSkill':
                    from tools.skill_improvement_experiment import _bodies
                    sid = selection['skillId']
                    descriptor = json.loads(manifest['packages'][sid]['descriptor.json'])
                    if selection['version'] != descriptor['version'] or len(row['calls']) != 2:
                        raise ValueError('READING_VERSION_OR_CALLS_CHANGED')
                    if row['loaded'] != _bodies(manifest['packages'][sid], sid, selection['sectionIds']):
                        raise ValueError('READING_PACKAGE_CHANGED')
                else:
                    raise ValueError('INVALID_RECORDED_SELECTION')
        for call in row['calls']:
            if call['requestHash'] != digest(call['request']):
                raise ValueError('REQUEST_CHANGED')
        rows[e['answerId']] = row
    judgments = {}
    for path in paths:
        review = json.loads(Path(path).read_text(encoding='utf-8'))
        ids = {r['answerId'] for r in review['answers']}
        subset = {**packet, 'items': [i for i in packet['items'] if i['answerId'] in ids]}
        # Failed generations have no score but remain part of the reviewed packet hash.
        candidates = [packet, subset]
        for part_path in directory.glob('blind-part-*.json'):
            part = json.loads(part_path.read_text(encoding='utf-8'))
            part_ids = {i['answerId'] for i in part['items']}
            authentic = {**packet, 'items': [i for i in packet['items'] if i['answerId'] in part_ids]}
            if part != authentic:
                raise ValueError('SPLIT_PACKET_CHANGED')
            candidates.append(part)
        bound = next((p for p in candidates if digest(p) == review.get('packetHash')), None)
        if bound is None:
            raise ValueError('UNKNOWN_REVIEW_PACKET')
        checked = validate(bound, review)
        if set(checked) & set(judgments):
            raise ValueError('DUPLICATE_REVIEW')
        judgments.update(checked)
    expected = {i['answerId'] for i in packet['items'] if i['hint'] is not None}
    if set(judgments) != expected:
        raise ValueError('INCOMPLETE_CONTENT_REVIEW')
    return manifest, rows, judgments


def developer_checks(directory, manifest, rows, judgments):
    path = Path(directory) / 'developer-review.json'
    developer = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    expected = {key for key, row in rows.items() if row['hint'] is not None}
    complete = (developer.get('experimentHash') == manifest['experimentHash']
                and developer.get('reviewer') == 'developer-AI' and developer.get('humanReviewed') is False
                and set(developer.get('reviewedAnswerIds', [])) == expected)
    unresolved = set(developer.get('unresolvedAnswerIds', [])) if complete else set()
    if not unresolved <= expected:
        raise ValueError('UNKNOWN_DISPUTE')
    adjusted = deepcopy(judgments)
    changes = []
    if complete:
        for change in developer.get('disputedReviewChecks', []):
            key, name = change['answerId'], change['checkId']
            if key not in adjusted or name not in adjusted[key]['checks']:
                raise ValueError('UNKNOWN_DEVELOPER_CHECK')
            if key in unresolved:
                continue  # Disputed answers are excluded entirely, never credited with a win.
            before = adjusted[key]['checks'][name]
            status = change['proposedStatus']
            if status == before['status']:
                continue  # Reason-only clarification stays in the developer audit.
            # Conservative corrections may remove apparent wins, never invent passes.
            if status not in ('fail', 'uncertain') or before['status'] == 'fail':
                raise ValueError('UNSUPPORTED_DEVELOPER_PROMOTION')
            if (not change.get('quote') or change['quote'] not in rows[key]['hint']
                    or not change.get('basis') or not change.get('reason')):
                raise ValueError('UNSUPPORTED_DEVELOPER_JUDGMENT')
            before.update(status=status, reason=change['reason'], quote=change['quote'])
            if name == 'helpfulness': before['level'] = 'uncertain' if status == 'uncertain' else 'partial'
            changes.append({'answerId':key,'checkId':name,'status':status,'reason':change['reason']})
    for key in unresolved:
        adjusted.pop(key, None)
    return complete, adjusted, changes


def summarize(directory, paths):
    manifest, rows, judgments = read_reviews(directory, paths)
    complete, judgments, changes = developer_checks(directory, manifest, rows, judgments)
    mapping = {(e['caseId'], e['round'], e['arm']): e['answerId'] for e in manifest['schedule']}
    pairs = [(a, b) for a, b in [('ordinary', 'full'), ('ordinary', 'selective'),
                                 ('full', 'selective'), ('selective-biased', 'selective')]
             if a in manifest['arms'] and b in manifest['arms']]
    comparisons = []
    for c in manifest['cases']:
        case = c['case']
        for n in (1, 2):
            def checks(arm):
                answer = mapping[case['id'], n, arm]
                return judgments.get(answer, {}).get('checks')
            comparisons.append({'caseId': case['id'], 'skillId': case['skillId'], 'round': n,
                                'pairs': {f'{b}Vs{a}': compare_checks(checks(a), checks(b)) for a, b in pairs}})
    held_out = bool(manifest['cases']) and all(c['case']['phase'] == 'holdout' for c in manifest['cases'])
    per_skill = {}
    stable = []
    stable_read = []
    for sid in sorted({c['case']['skillId'] for c in manifest['cases']}):
        subset = [r for r in comparisons if r['skillId'] == sid]
        local = [cid for cid in sorted({r['caseId'] for r in subset})
                 if all(r['pairs'].get('selectiveVsordinary') == 'improvement' for r in subset if r['caseId'] == cid)]
        local_read = [cid for cid in local if all(
            rows[mapping[cid, n, 'selective']]['loaded'] is not None
            and (rows[mapping[cid, n, 'selective']].get('selection') or {}).get('skillId') == sid
            for n in (1, 2))]
        full_stable = [cid for cid in sorted({r['caseId'] for r in subset})
                       if all(r['pairs'].get('fullVsordinary') == 'improvement' for r in subset if r['caseId'] == cid)]
        full_no_regression = not any(r['pairs'].get('fullVsordinary') == 'regression' for r in subset)
        stable.extend(local); stable_read.extend(local_read)
        no_regression = not any(r['pairs'].get('selectiveVsordinary') == 'regression' for r in subset)
        per_skill[sid] = {'stableFullImprovementCases': full_stable,
                         'fullBenefitGateMet': held_out and complete and len(full_stable) >= 2 and full_no_regression,
                         'expansionGateMet': held_out and complete and len(local) >= 2 and no_regression,
                         'readingBenefitGateMet': held_out and complete and len(local_read) >= 2 and no_regression,
                         'comparisons': {f'{b}Vs{a}': dict(Counter(r['pairs'][f'{b}Vs{a}'] for r in subset)) for a, b in pairs},
                         'stableImprovementCases': local, 'stableImprovementWithReading': local_read}
    return {'experimentHash': manifest['experimentHash'], 'humanReviewed': False,
            'reviewKind': 'independent-AI', 'developerReviewComplete': complete,
            'conservativeDeveloperCorrections': changes,
            'comparisons': comparisons, 'perSkill': per_skill,
            'counts': {f'{b}Vs{a}': dict(Counter(r['pairs'][f'{b}Vs{a}'] for r in comparisons)) for a, b in pairs},
            'stableSelectiveImprovementCases': stable, 'stableImprovementWithReading': stable_read,
            'expansionGateMet': bool(per_skill) and all(s['expansionGateMet'] for s in per_skill.values()),
            'readingBenefitGateMet': bool(per_skill) and all(s['readingBenefitGateMet'] for s in per_skill.values())}


def compare_versions(old_directory, new_directory, old_paths, new_paths, *, arm='full'):
    old_m, old_rows, old_j = read_reviews(old_directory, old_paths)
    new_m, new_rows, new_j = read_reviews(new_directory, new_paths)
    if old_m['modelConfig'] != new_m['modelConfig']:
        raise ValueError('DIFFERENT_MODEL_CONFIG')
    _, old_j, _ = developer_checks(old_directory, old_m, old_rows, old_j)
    _, new_j, _ = developer_checks(new_directory, new_m, new_rows, new_j)
    old_cases = {c['case']['id']: c['payload'] for c in old_m['cases']}
    new_cases = {c['case']['id']: c['payload'] for c in new_m['cases']}
    shared = set(old_cases) & set(new_cases)
    if not shared or any(old_cases[key] != new_cases[key] for key in shared):
        raise ValueError('DIFFERENT_OR_EMPTY_SHARED_MATERIAL')
    def indexed(manifest, judgments):
        return {(e['caseId'], e['round']): judgments.get(e['answerId'], {}).get('checks')
                for e in manifest['schedule'] if e['arm'] == arm and e['caseId'] in shared}
    a, b = indexed(old_m, old_j), indexed(new_m, new_j)
    if set(a) != set(b):
        raise ValueError('DIFFERENT_REPEAT_COVERAGE')
    return {'oldExperiment': old_m['experimentHash'], 'newExperiment': new_m['experimentHash'],
            'arm': arm, 'comparisons': [{'caseId': key[0], 'round': key[1],
                                      'verdict': compare_checks(a[key], b[key])} for key in sorted(a)]}
