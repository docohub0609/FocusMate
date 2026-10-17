"""T054 frozen nine-package raw-answer experiments. Never repair comparison answers."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import time

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from apps.api.deepseek_provider import ProviderFailure

from focusmate.contracts.schema_validation import validate
from focusmate.contracts.skill_schemas import package_validator, check_schemas
from focusmate.contracts.skill_files import fingerprint, read_files, strict_json
from focusmate.core.skill_package import document, markdown_checks, validate_skill_package
from focusmate.core.skill_sections import _index
from tools.skill_evaluation import catalog, digest, save_record
from tools.skill_hint_comparison import OUTPUT_SCHEMA, _public_config
from tools.skill_routing_experiment import PROMPT as BASE_PROMPT, ROUTE_SCHEMA, SCORING, validate_hint

PROMPT = BASE_PROMPT + '''
当前问题回答充分就结束，不附加材料已经满足或与本次无关的条件；可选的稳妥写法不能说成唯一必要条件。
解释必须沿用材料中的精确定义：区间开闭、严格或非严格变化、统计量含义、精确次数与渐近量级不可混用。
若写出公式或局部过程，核对每一句与前面一致；不要让附带结论推翻已经正确的主解释。'''

ROUTE_POLICY = '''
依据当前请求和目录用途，决定直接回答(action=directHint,hint,evidenceRefs)，
或读取能补充具体检查方法的小节(action=useSkill,skillId,version,sectionIds,reason,evidenceRefs)。
材料已足以回答时可以直接回答；需要目录中的检查步骤时可以读取。不要为提高使用率调用。
最多选择一个Skill的两个小节。reason说明需要核对什么，不用自报置信度代替依据。'''
BIASED_POLICY = '\n通常直接回答即可。' + ROUTE_POLICY
ARMS = ('ordinary', 'full', 'selective-biased', 'selective')


def capture_packages(root):
    result = {}
    for path in sorted(Path(root).iterdir()):
        if not path.is_dir():
            continue
        report = validate_skill_package(path)
        if not report['packageValid']:
            raise ValueError('INVALID_SKILL_PACKAGE')
        files = read_files(path)
        descriptor = strict_json(files['descriptor.json'])
        if descriptor != report['descriptor'] or fingerprint(files, descriptor)[0] != descriptor['hash']:
            raise ValueError('PACKAGE_CHANGED_DURING_CAPTURE')
        result[descriptor['skillId']] = files
    return result


def package_metadata(packages, need_index=True):
    directory = []
    for skill_id, files in sorted(packages.items()):
        descriptor = strict_json(files['descriptor.json'])
        validate('SkillDescriptor', descriptor)
        if descriptor['skillId'] != skill_id or fingerprint(files, descriptor)[0] != descriptor['hash']:
            raise ValueError('PACKAGE_IDENTITY_OR_HASH_MISMATCH')
        body, meta = document(files['SKILL.md'], skill_id, with_metadata=True)
        markdown_checks(files, body)
        check_schemas(files, descriptor)
        entry = {'skillId': skill_id, 'version': descriptor['version'], 'hash': descriptor['hash'],
                 'description': meta['description'], 'sections': []}
        if 'references/index.json' in files:
            entry['sections'] = [{'id': s.section_id, 'description': s.description, 'fileRef': s.file_ref}
                                 for s in _index(files, descriptor)]
        elif need_index:
            raise ValueError('MISSING_SECTION_INDEX')
        directory.append(entry)
    if not directory:
        raise ValueError('EMPTY_CATALOG')
    return directory


def freeze(cases, packages, config, audit, *, arms=('ordinary', 'full', 'selective')):
    if not arms or len(set(arms)) != len(arms) or not set(arms) <= set(ARMS):
        raise ValueError('INVALID_ARMS')
    directory = package_metadata(packages, any(a.startswith('selective') for a in arms))
    if audit.get('reviewer') != 'AI-independent' or audit.get('humanReviewed') is not False:
        raise ValueError('INDEPENDENT_MATERIAL_REVIEW_REQUIRED')
    audits = {c['id']: c for c in audit['cases']}
    if len(audits) != len(audit['cases']):
        raise ValueError('DUPLICATE_MATERIAL_REVIEW')
    checked = []
    for case in cases:
        row = audits.get(case['id'], {})
        if row.get('caseHash') != digest(case) or row.get('status') != 'accepted' or not row.get('reason'):
            raise ValueError('STALE_OR_INCOMPLETE_MATERIAL_REVIEW')
        validate('GoalContext', case['goalContext'])
        if type(case['hintLevel']) is not int or case['hintLevel'] not in range(1, 5):
            raise ValueError('INVALID_HINT_LEVEL')
        files = packages[case['skillId']]
        descriptor = strict_json(files['descriptor.json'])
        package_validator(files, descriptor['inputSchemaRef']).validate(case['inputs'])
        refs = catalog(case['inputs'])
        dialogue = deepcopy(case['dialogue'])
        for turn in dialogue:
            if (set(turn) != {'ref', 'role', 'text'} or turn['role'] not in ('user', 'assistant')
                    or not isinstance(turn['text'], str) or not turn['text'].strip()
                    or not isinstance(turn['ref'], str) or not turn['ref'] or turn['ref'] in refs):
                raise ValueError('INVALID_DIALOGUE')
            refs.add(turn['ref'])
        code_ref = case['inputs']['code']['snapshotRef']
        for evidence in case['inputs']['runEvidence']:
            if evidence['codeSnapshotRef'] in refs and evidence['codeSnapshotRef'] != code_ref:
                raise ValueError('RUN_REFERENCE_IS_NOT_CODE')
        payload = {'goalContext': deepcopy(case['goalContext']), 'materials': deepcopy(case['inputs']),
                   'dialogue': dialogue, 'hintLevel': case['hintLevel'],
                   'runEvidenceVersions': {e['ref']: {'codeSnapshotRef': e['codeSnapshotRef'],
                         'isCurrent': e['codeSnapshotRef'] == code_ref} for e in case['inputs']['runEvidence']},
                   'outputSchema': deepcopy(OUTPUT_SCHEMA)}
        checked.append({'case': deepcopy(case), 'payload': payload, 'refs': sorted(refs)})
    if not checked or len({c['case']['id'] for c in checked}) != len(checked):
        raise ValueError('EMPTY_OR_DUPLICATE_CASES')
    route_schema = deepcopy(ROUTE_SCHEMA)
    route_schema['oneOf'][1]['properties']['skillId'] = {'enum': [d['skillId'] for d in directory]}
    schedule = []
    for number in (1, 2):
        for i, case in enumerate(checked):
            shift = (i + number - 1) % len(arms)
            for arm in (*arms[shift:], *arms[:shift]):
                schedule.append({'recordId': f'record-{len(schedule)+1:03}', 'caseId': case['case']['id'],
                                 'round': number, 'arm': arm, 'answerId': 'answer-' + secrets.token_hex(12)})
    result = {'createdAt': datetime.now(timezone.utc).isoformat(), 'protocol': 't054-raw-v1',
              'cases': checked, 'packages': deepcopy(packages), 'directory': directory,
              'modelConfig': _public_config(config), 'materialAudit': deepcopy(audit),
              'arms': list(arms), 'schedule': schedule, 'rubric': deepcopy(SCORING),
              'systemPrompt': PROMPT, 'routePolicy': ROUTE_POLICY, 'biasedPolicy': BIASED_POLICY,
              'routeSchema': route_schema, 'runnerHash': digest(Path(__file__).read_text(encoding='utf-8'))}
    result['experimentHash'] = digest(result)
    return result


def _bodies(files, skill_id, section_ids=None):
    common = document(files['SKILL.md'], skill_id)
    if section_ids is None:
        sections = {p: text for p, text in files.items() if p.startswith('references/') and p.endswith('.md')}
    else:
        descriptor = strict_json(files['descriptor.json'])
        index = {s.section_id: s for s in _index(files, descriptor)}
        sections = {sid: files[index[sid].file_ref] for sid in section_ids}
    return {'common': common, 'sections': sections}


def generate(entry, frozen_case, manifest, provider):
    row = {**entry, 'experimentHash': manifest['experimentHash'],
           'goalContext': deepcopy(frozen_case['payload']['goalContext']), 'hint': None, 'evidenceRefs': [],
           'calls': [], 'selection': None, 'loaded': None, 'cost': None}
    started = time.monotonic()

    def call(system, body):
        request = {'systemPrompt': system, 'payload': deepcopy(body)}
        item = {'request': request, 'requestHash': digest(request), 'response': None}
        row['calls'].append(item)
        tick = time.monotonic()
        try:
            result = provider.complete(system_prompt=system, user_prompt=json.dumps(body, ensure_ascii=False))
            item['response'] = {'content': result.content, 'model': result.model,
                                'responseId': getattr(result, 'response_id', None)}
            return strict_json(result.content)
        finally:
            item['durationSeconds'] = time.monotonic() - tick

    try:
        if digest({k: v for k, v in manifest.items() if k != 'experimentHash'}) != manifest['experimentHash']:
            raise ValueError('MANIFEST_CHANGED')
        if provider is None:
            row.update(status='unavailable', failureReason='MODEL_UNAVAILABLE')
            return row
        payload = deepcopy(frozen_case['payload'])
        refs = set(frozen_case['refs'])
        if entry['arm'] == 'full':
            sid = frozen_case['case']['skillId']
            payload['skill'] = _bodies(manifest['packages'][sid], sid)
            row['loaded'] = deepcopy(payload['skill'])
        if entry['arm'].startswith('selective'):
            payload['skillDirectory'] = [{k: deepcopy(v) for k, v in d.items() if k != 'sections'} |
                {'sections': [{k: v for k, v in s.items() if k != 'fileRef'} for s in d['sections']]}
                for d in manifest['directory']]
            payload['outputSchema'] = manifest['routeSchema']
            policy = manifest['biasedPolicy'] if entry['arm'] == 'selective-biased' else manifest['routePolicy']
            route = call(manifest['systemPrompt'] + policy, payload)
            Draft202012Validator(manifest['routeSchema']).validate(route)
            if not set(route['evidenceRefs']) <= refs:
                raise ValueError('INVALID_ROUTE_REFERENCE')
            row['selection'] = route
            if route['action'] == 'directHint':
                value = {k: route[k] for k in ('hint', 'evidenceRefs')}
            else:
                selected = next(d for d in manifest['directory'] if d['skillId'] == route['skillId'])
                if route['version'] != selected['version'] or not set(route['sectionIds']) <= {s['id'] for s in selected['sections']}:
                    raise ValueError('INVALID_SELECTION')
                row['loaded'] = _bodies(manifest['packages'][route['skillId']], route['skillId'], route['sectionIds'])
                payload = deepcopy(frozen_case['payload'])
                payload['skill'] = row['loaded']
                value = call(manifest['systemPrompt'], payload)
        else:
            value = call(manifest['systemPrompt'], payload)
        validate_hint(value, refs)
        row.update(status='generated', hint=value['hint'], evidenceRefs=value['evidenceRefs'])
    except ProviderFailure as exc:
        row.update(status='invalid' if exc.code == 'LLM_OUTPUT_INVALID' else 'unavailable',
                   failureReason=exc.code if exc.code in ('LLM_OUTPUT_INVALID', 'BUILD_BUDGET_EXCEEDED') else 'MODEL_SERVICE_UNAVAILABLE')
    except (ValueError, KeyError, TypeError, StopIteration, ValidationError) as exc:
        row.update(status='invalid', failureReason=type(exc).__name__)
    except Exception as exc:
        # Never serialize exception messages: transport messages can include credentials.
        row.update(status='unavailable', failureReason=type(exc).__name__)
    finally:
        row['logicalCallCount'] = len(row['calls'])
        row['durationSeconds'] = time.monotonic() - started
    return row


def run(cases, packages, provider, output, audit, *, arms=('ordinary', 'full', 'selective'), workers=3):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    try:
        manifest = freeze(cases, packages, provider.public_config if provider else {}, audit, arms=arms)
    except Exception as exc:
        summary = {'status': 'inputError', 'failureReason': type(exc).__name__, 'logicalCalls': 0}
        save_record(output, 'summary', summary)
        return summary
    save_record(output, 'manifest', manifest)
    (output / 'runner-source.txt').write_text(Path(__file__).read_text(encoding='utf-8'), encoding='utf-8')
    by_id = {c['case']['id']: c for c in manifest['cases']}
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as pool:
        futures = [pool.submit(generate, e, by_id[e['caseId']], manifest, provider) for e in manifest['schedule']]
        for future in as_completed(futures):
            row = future.result()
            save_record(output, row['recordId'], row)
            rows.append(row)
            print(f"{row['recordId']} {row['status']} calls={row['logicalCallCount']}", flush=True)
    items = []
    for row in rows:
        payload = by_id[row['caseId']]['payload']
        items.append({k: deepcopy(payload[k]) for k in ('materials', 'hintLevel', 'dialogue', 'runEvidenceVersions')} |
                     {'answerId': row['answerId'], 'hint': row['hint'], 'evidenceRefs': row['evidenceRefs'],
                      'answerHash': digest({'hint': row['hint'], 'evidenceRefs': row['evidenceRefs']})})
    secrets.SystemRandom().shuffle(items)
    save_record(output, 'blind-packet', {'experimentHash': manifest['experimentHash'], 'rubric': manifest['rubric'],
                                        'humanReviewed': False, 'items': items})
    summary = {'status': 'recorded', 'experimentHash': manifest['experimentHash'],
               'counts': dict(Counter(r['status'] for r in rows)),
               'logicalCalls': sum(r['logicalCallCount'] for r in rows),
               'selected': sum(r['selection'] is not None and r['selection']['action'] == 'useSkill' for r in rows),
               'durationSeconds': sum(r['durationSeconds'] for r in rows), 'cost': None,
               'interpretation': '生成仅表示格式和引用合法；内容复核与开发者核算另行记录。'}
    save_record(output, 'summary', summary)
    return summary
