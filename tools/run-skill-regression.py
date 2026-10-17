"""T054 fixed-reviewer calibration and historical 54-case executor regression."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.skill_execution import DeepSeekSkillExecutionProvider
from focusmate.core.skill_execution import execute_skill, review_candidate, ACTOR_PROMPT, REVIEW_PROMPT
from tools.skill_evaluation import apply_independent_review, digest, save_record
from tools.skill_improvement_experiment import capture_packages


def exit_status(summary):
    count = summary.get('records', 0)
    if not count:
        return 1
    passed = (summary.get('matched') == count if summary['mode'] == 'calibration' else
              summary.get('statuses', {}).get('accepted') == count)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description='T054真实审核器校准或54例回归，预期不进入模型。')
    parser.add_argument('--mode', choices=['calibration', 'regression'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--pair-id', action='append')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--case-audit', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出目录已存在，不覆盖。')
    if args.mode == 'calibration':
        from tools.skill_improvement_cases import get_reviewer_cases
        cases = get_reviewer_cases()
        audit = json.loads(args.case_audit.read_text(encoding='utf-8'))
        by_id = {c['id']: c for c in audit['cases']}
        if audit['reviewer'] != 'AI-independent' or audit['humanReviewed'] is not False:
            parser.error('校准材料缺独立审核。')
        for c in cases:
            if by_id[c['id']]['caseHash'] != digest(c) or by_id[c['id']]['status'] != 'accepted':
                parser.error('校准材料变更，需重新核对。')
        if args.pair_id:
            if not set(args.pair_id) <= {c['pairId'] for c in cases}:
                parser.error('未知对照组。')
            cases = [c for c in cases if c['pairId'] in args.pair_id]
        repeats = 2
    else:
        from tools.skill_evaluation_cases import build_cases
        audit = json.loads(args.case_audit.read_text(encoding='utf-8'))
        source = (ROOT / 'tools/skill_evaluation_cases.py').read_text(encoding='utf-8')
        cases = apply_independent_review(build_cases(), audit, hashlib.sha256(source.encode()).hexdigest())
        repeats = 1
    packages = capture_packages(ROOT / 'skills')
    descriptors = {k: json.loads(v['descriptor.json']) for k, v in packages.items()}
    if args.env_file:
        load_local_env(args.env_file)
    provider = DeepSeekSkillExecutionProvider.from_environment()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {'mode': args.mode, 'cases': cases, 'materialAudit': audit, 'packages': packages,
                'modelConfig': provider.public_config, 'actorPrompt': ACTOR_PROMPT, 'reviewPrompt': REVIEW_PROMPT,
                'executorSource': (ROOT / 'src/focusmate/core/skill_execution.py').read_text(encoding='utf-8'),
                'repeats': repeats, 'humanReviewed': False}
    manifest['experimentHash'] = digest(manifest)
    save_record(args.output, 'manifest', manifest)

    def one(index, case, number):
        descriptor = descriptors[case['skillId']]
        request = {'goalContext': case['goalContext'], 'skillRef': {
            'skillId': descriptor['skillId'], 'skillVersion': descriptor['version'], 'skillHash': descriptor['hash']},
            'inputs': case['inputs'], 'hintLevel': case.get('hintLevel', 2), 'dialogue': case.get('dialogue', [])}
        prefix = f'record-{index:03}'
        save_record(args.output, prefix + '-input', request)
        counter = 0
        def sink(event):
            nonlocal counter
            counter += 1
            save_record(args.output, f'{prefix}-event-{counter:02}', event)
        if args.mode == 'calibration':
            report = review_candidate(request, case['analysis'], provider, audit_sink=sink)
        else:
            report = execute_skill(request, provider, audit_sink=sink)
        row = {'caseId': case['id'], 'round': number, 'goalContext': case['goalContext'],
               'skillId': case['skillId'], 'experimentHash': manifest['experimentHash'], 'report': report}
        if args.mode == 'calibration':
            actual = ('pass' if report['status'] == 'accepted' else
                      'fail' if report.get('failure', {}).get('reason') == 'REVIEW_FAILED' else 'incomplete')
            row.update(expectedVerdict=case['expectedVerdict'], actualVerdict=actual,
                       matched=actual == case['expectedVerdict'], pairId=case['pairId'])
        save_record(args.output, prefix, row)
        print(f"{prefix} {report['status']} {report.get('failure', {}).get('reason', '')}", flush=True)
        return row

    jobs = [(c, n) for n in range(1, repeats + 1) for c in cases]
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        futures = [pool.submit(one, i, c, n) for i, (c, n) in enumerate(jobs, 1)]
        for future in as_completed(futures):
            rows.append(future.result())
    summary = {'experimentHash': manifest['experimentHash'], 'mode': args.mode, 'records': len(rows),
               'statuses': dict(Counter(r['report']['status'] for r in rows)),
               'failures': dict(Counter(r['report']['failure']['reason'] for r in rows if 'failure' in r['report'])),
               'logicalCalls': sum(r['report']['logicalRequests'] for r in rows),
               'acceptedAfterRepair': sum(r['report']['status'] == 'accepted' and r['report']['logicalRequests'] > 2 for r in rows),
               'cost': None, 'humanReviewed': False}
    if args.mode == 'calibration':
        summary.update(matched=sum(r['matched'] for r in rows), failedPairs=sorted({r['pairId'] for r in rows if not r['matched']}))
    save_record(args.output, 'summary', summary)
    print(json.dumps(summary, ensure_ascii=False))
    return exit_status(summary)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
