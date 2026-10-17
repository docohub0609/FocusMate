"""Frozen developer scenarios. Expected judgments never reach the dialogue runner."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {'sufficient', 'search_read', 'direct_read', 'old_submission', 'public_run', 'fault_permission'}


def load_cases(path=None):
    return json.loads(Path(path or ROOT/'tests/fixtures/t057/tool-cases.json').read_text(encoding='utf-8'))


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def freeze_experiment(cases, config, group='all'):
    cases = deepcopy(cases)
    if len(cases) != 24 or len({c['id'] for c in cases}) != 24:
        raise ValueError('24 unique scenarios required')
    for split in ('development', 'holdout'):
        if Counter(c['category'] for c in cases if c['group'] == split) != {k: 2 for k in CATEGORIES}:
            raise ValueError('six categories with two scenarios per split required')
    for case in cases:
        material = case['input']['materials']
        refs = [material['userRequest']['ref']] + [p['ref'] for p in material['problemFacts']]
        if 'codeExcerpt' in material:
            refs.append(material['codeExcerpt']['ref'])
        if len(refs) != len(set(refs)) or not all(re.fullmatch(r'[qpcs][0-9]+', ref) for ref in refs):
            raise ValueError('unique neutral references required')
        if any(k in material for k in ('expected', 'category', 'fixtures', 'group')):
            raise ValueError('evaluation information in model materials')
        if not case['expected']['basis'] or not case['expected']['forbidden']:
            raise ValueError('expectations need reasons and forbidden conclusions')
    if group not in ('all', 'development', 'holdout'):
        raise ValueError('invalid group')
    selected = [c for c in cases if group == 'all' or c['group'] == group]
    schedule = [{'caseId': c['id'], 'repeat': r, 'recordId': f"{c['id']}-{r}"}
                for c in selected for r in range(1, 3 if c['group'] == 'holdout' else 2)]
    safe = {k: deepcopy(v) for k, v in config.items() if k in
            {'model', 'provider', 'maxTokens', 'thinkingEnabled', 'timeoutSeconds', 'networkAttempts', 'promptVersion'}}
    result = {'version': 't057-evaluation-1', 'group': group, 'modelConfig': safe,
              'cases': cases, 'schedule': schedule, 'cost': None,
              'rubric': ['toolSelection', 'execution', 'materialAssociation', 'resultInterpretation'],
              'reviewStatus': 'pending', 'humanConfirmed': False}
    from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
    from focusmate.core.tool_dialogue import TOOL_PROMPT
    result['tools'] = deepcopy(TOOL_DEFINITIONS)
    result['systemPrompt'] = TOOL_PROMPT
    result['implementationHashes'] = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for pattern in ('src/focusmate/core/tool*.py', 'src/focusmate/adapters/tool*.py',
                        'src/focusmate/contracts/tool*.py', 'services/oj_worker/tool*.py', 'tools/tool_demo.py')
        for path in ROOT.glob(pattern)}
    result['contentHash'] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result


def run_evaluation(cases, runner, output_dir, config=None, group='all'):
    manifest = freeze_experiment(cases, config or {}, group)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    (output/'records').mkdir()
    save(output/'manifest.json', manifest)
    indexed = {c['id']: c for c in manifest['cases']}
    counts = Counter()
    for row in manifest['schedule']:
        directory = output/'records'/row['recordId']
        directory.mkdir()
        start = time.monotonic()
        try:
            report = runner(deepcopy(indexed[row['caseId']]['input']), directory)
            if not isinstance(report, dict) or not isinstance(report.get('status'), str):
                raise ValueError('invalid runner report')
        except Exception as exc:
            # Provider messages may contain endpoint credentials; exception type is enough here.
            report = {'status': 'runner_failed', 'errorType': type(exc).__name__}
        counts[report['status']] += 1
        save(output/'records'/f"{row['recordId']}.json", {
            **row, 'durationSeconds': time.monotonic()-start, 'report': report,
            'reviewStatus': 'pending', 'accepted': None})
        print(f"{row['recordId']}: {report['status']}", flush=True)
    result = {'counts': dict(counts), 'contentAccepted': None, 'cost': None,
              'note': 'Execution completion is not semantic acceptance. Review original materials and answers separately.'}
    save(output/'summary.json', result)
    return result
