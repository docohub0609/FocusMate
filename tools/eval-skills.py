"""Explicit developer-only Skill analysis and independent review."""
import argparse
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import ProviderFailure, load_local_env
from focusmate.adapters.skill_evaluation import DeepSeekSkillEvaluationProvider
from focusmate.core.skill_package import validate_skill_package
from tools.skill_evaluation import ACTOR_PROMPT, REVIEW_PROMPT, PROMPT_VERSION, RUBRIC_VERSION, REVIEW_PROTOCOL_VERSION, digest, evaluate_case, save_record, valid_case_id, apply_independent_review


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立开发评测：每例一次分析、一次审核；不更新学习进度。')
    parser.add_argument('--skill', required=True, help='具体 Skill ID 或 all')
    parser.add_argument('--split', required=True, choices=['development', 'holdout'])
    parser.add_argument('--output', type=Path, required=True, help='新的结果目录，不覆盖历史')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--regression', action='store_true', help='使用已经看过结果的材料做回归，不称首次保留验收')
    parser.add_argument('--case-audit', type=Path, required=True, help='Explicit reviewed case record; never bundled user history')
    parser.add_argument('--approval', type=Path, required=True, help='Explicit calibration approval record')
    args = parser.parse_args(argv)
    from tools.skill_evaluation_cases import build_cases
    try:
        audit = json.loads(args.case_audit.read_text(encoding='utf-8'))
        source_hash = hashlib.sha256((ROOT / 'tools/skill_evaluation_cases.py').read_text(encoding='utf-8').encode()).hexdigest()
        reviewed_cases = apply_independent_review(build_cases(), audit, source_hash)
    except (OSError, ValueError, KeyError):
        print('案例独立复核未完成或材料已变化，未发送请求。', flush=True)
        return 1
    cases = [case for case in reviewed_cases if case['split'] == args.split
             and (args.skill == 'all' or case['skillId'] == args.skill)]
    if not cases:
        print('没有匹配的案例。', flush=True)
        return 1
    # This tracked record records the actual user's calibration approval, not model approval.
    approval_path = args.approval
    try:
        approval = json.loads(approval_path.read_text(encoding='utf-8'))
        if approval.get('representativeScaleApproved') is not True:
            raise ValueError('representatives pending')
        args.output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError):
        print('评测未启动：确认记录不可用，或结果目录已存在。', flush=True)
        return 1
    ids = [case.get('id') for case in cases]
    if not all(valid_case_id(value) for value in ids) or len(ids) != len({value.lower() for value in ids}):
        save_record(args.output, 'input-failure', {'status': 'inputError', 'reason': 'INVALID_OR_DUPLICATE_CASE_ID'})
        print('案例编号无效或重复，未发送请求。', flush=True)
        return 1
    if args.env_file: load_local_env(args.env_file)
    try:
        provider = DeepSeekSkillEvaluationProvider.from_environment()
    except ProviderFailure:
        save_record(args.output, 'configuration-failure', {'status': 'unavailable', 'reason': 'MODEL_CONFIGURATION_UNAVAILABLE'})
        print('模型配置不可用，未发送请求。', flush=True)
        return 1
    package_reports = {skill: validate_skill_package(ROOT / 'skills' / skill)
                       for skill in sorted({case['skillId'] for case in cases})}
    manifest = {'createdAt': datetime.now(timezone.utc).isoformat(), 'split': args.split,
                'evaluationUse': 'regression' if args.regression else ('firstHoldout' if args.split == 'holdout' else 'development'),
                'caseIds': [case['id'] for case in cases], 'casesHash': digest(cases),
                'modelConfig': provider.public_config, 'promptVersion': PROMPT_VERSION,
                'rubricVersion': RUBRIC_VERSION, 'reviewProtocolVersion': REVIEW_PROTOCOL_VERSION, 'actorPrompt': ACTOR_PROMPT, 'reviewPrompt': REVIEW_PROMPT,
                'packages': package_reports, 'representativeApproval': approval,
                'holdoutPolicy': '运行前固定本记录；据此修改 Skill 后，本批转为回归，不删除首次结果。'}
    save_record(args.output, 'manifest', manifest)
    counts = {}
    for case in cases:
        # Detect accidental edits during a paid run; never silently evaluate a moving package.
        current = validate_skill_package(ROOT / 'skills' / case['skillId'])
        if current != package_reports[case['skillId']]:
            row = {'id': case['id'], 'goalContext': case['goalContext'], 'status': 'inputError',
                   'failureReason': 'SKILL_CHANGED_AFTER_RUN_FREEZE'}
        else:
            row = evaluate_case(case, ROOT / 'skills', provider)
        row['modelConfig'] = provider.public_config
        save_record(args.output, case['id'], row)
        counts[row['status']] = counts.get(row['status'], 0) + 1
        print(f"{case['id']}: {row['status']}", flush=True)
    save_record(args.output, 'summary', {'caseCount': len(cases), 'counts': counts,
                'completed': True, 'allPassed': counts.get('passed', 0) == len(cases),
                'scope': '仅说明所选案例表现，未经用户目标验收，不代表对所有题目有效。'})
    return 0 if counts.get('passed', 0) == len(cases) else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
