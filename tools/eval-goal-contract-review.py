"""Run the explicitly labelled T042 evaluation without touching a business DB."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import ProviderFailure, load_local_env
from tools.goal_contract_review_cases import build_cases


def select_cases(cases, split):
    if split not in ('development', 'holdout', 'all'):
        raise ValueError('Unsupported evaluation split.')
    return [case for case in cases if split == 'all' or case['split'] == split]


def _default_review(*args, **kwargs):
    from focusmate.core.goal_contract_review import review_goal_contract
    return review_goal_contract(*args, **kwargs)


def evaluate_cases(cases, provider, *, reviewer=None, on_result=None):
    """Sequential requests; labels and provenance never cross the review boundary."""
    reviewer = reviewer or _default_review
    started_at = datetime.now(timezone.utc).isoformat()
    run_started = time.monotonic()
    results = []
    for case in cases:
        case_started = time.monotonic()
        inputs = deepcopy(case['inputs'])
        review = None
        error_code = None
        try:
            review = reviewer(inputs['goal_record'], inputs['problem_record'], inputs['feature_set'],
                              inputs['compilation'], provider,
                              created_at=datetime.now(timezone.utc).isoformat())
            actual = review['status']
            if actual not in ('passed', 'needsReview', 'unavailable'):
                raise ValueError('Invalid review status.')
        except Exception as error:
            code = getattr(error, 'code', '')
            # Invalid fixtures are evaluation failures, never model detections.
            actual = 'inputError' if code == 'INVALID_GOAL_CONTRACT_REVIEW_INPUT' else 'evaluationError'
            error_code = 'INVALID_EVALUATION_INPUT' if actual == 'inputError' else 'EVALUATION_FAILED'
            review = None
        expected = deepcopy(case['expected'])
        matching_issues = [issue for issue in (review or {}).get('issues', [])
                           if issue.get('type') in expected.get('acceptableIssueTypes', [])
                           and issue.get('itemId') in expected.get('acceptableItemIds', [])]
        issue_match = bool(expected.get('issueType') and actual == 'needsReview' and matching_issues)
        matched = actual == expected['status'] and (not expected.get('issueType') or issue_match)
        row = {'id': case['id'], 'split': case['split'], 'inputs': deepcopy(case['inputs']),
               'expected': expected, 'provenance': deepcopy(case['provenance']),
               'actual': actual, 'matched': matched, 'expectedIssueMatch': issue_match,
               'review': review, 'model': (review or {}).get('model'),
               'elapsedSeconds': round(time.monotonic() - case_started, 3)}
        if error_code:
            row['errorCode'] = error_code
        results.append(row)
        if on_result:
            on_result(_summary(cases, results, started_at, run_started, completed=False))
        print(f"{case['id']}: expected={expected['status']}, actual={actual}, issueMatch={issue_match}", flush=True)
    return _summary(cases, results, started_at, run_started, completed=True)


def _summary(cases, results, started_at, run_started, *, completed):
    return {
        'createdAt': started_at, 'caseCount': len(cases), 'processedCount': len(results), 'completed': completed,
        'matched': sum(row['matched'] for row in results),
        'missedError': sum(row['expected']['status'] == 'needsReview' and row['actual'] == 'passed' for row in results),
        'falseAlarm': sum(row['expected']['status'] == 'passed' and row['actual'] == 'needsReview' for row in results),
        'unavailable': sum(row['actual'] == 'unavailable' for row in results),
        'inputError': sum(row['actual'] == 'inputError' for row in results),
        'evaluationError': sum(row['actual'] == 'evaluationError' for row in results),
        'expectedIssueMatch': sum(row['expectedIssueMatch'] for row in results),
        'models': sorted({row['model'] for row in results if row['model']}),
        'promptVersions': sorted({row['review']['promptVersion'] for row in results
                                  if row['review'] and row['review'].get('promptVersion')}),
        'elapsedSeconds': round(time.monotonic() - run_started, 3),
        'casesHash': hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode('utf-8')).hexdigest(),
        'scope': '24 AI-prepared synthetic cases. Only the six development mutation judgments were approved as representatives; this is not a general-accuracy benchmark.',
        'holdoutPolicy': 'Keep prompts fixed before first holdout use. Any tuning on a holdout result makes those cases regression cases.',
        'results': results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='真实审核完整合同案例，分别记录漏报、误报和不可用。')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--split', choices=('development', 'holdout', 'all'), default='development')
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args(argv)
    log_path = args.output.with_name(args.output.name + '.jsonl')
    if args.output.exists() or log_path.exists():
        print('输出文件或逐案日志已存在，请使用新路径以保留历史。', flush=True)
        return 1
    try:
        if args.env_file:
            load_local_env(args.env_file)
        from focusmate.adapters.goal_contract_review import DeepSeekGoalContractReviewProvider
        provider = DeepSeekGoalContractReviewProvider.from_environment()
        cases = select_cases(build_cases(), args.split)
        # Reserve a new output path before incurring model-call costs.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as target, log_path.open('x', encoding='utf-8') as log:
            def save_summary(value):
                value = {**value, 'split': args.split, 'resultLog': str(log_path)}
                rendered = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
                # This handle owns a newly reserved file, never existing history.
                target.seek(0)
                target.write(rendered)
                target.truncate()
                target.flush()
                os.fsync(target.fileno())

            def save_result(partial):
                # Append first: a process crash while replacing the summary still
                # leaves every completed response recoverable from this log.
                log.write(json.dumps(partial['results'][-1], ensure_ascii=False) + '\n')
                log.flush()
                os.fsync(log.fileno())
                save_summary(partial)

            save_summary(_summary(cases, [], datetime.now(timezone.utc).isoformat(),
                                  time.monotonic(), completed=False))
            result = evaluate_cases(cases, provider, on_result=save_result)
            save_summary(result)
        print(f"核对一致 {result['matched']}/{result['caseCount']}，漏报 {result['missedError']}，误报 {result['falseAlarm']}，审核不可用 {result['unavailable']}，输入错误 {result['inputError']}。", flush=True)
        return 0 if result['matched'] == result['caseCount'] else 1
    except (OSError, ValueError, ProviderFailure):
        print('评测未完成：请检查模型配置、输入或输出路径。', flush=True)
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
