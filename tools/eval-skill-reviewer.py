"""Developer-only critic calibration. One request per fixed analysis."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import ProviderFailure, load_local_env
from focusmate.adapters.skill_evaluation import DeepSeekSkillEvaluationProvider
from focusmate.contracts.skill_files import strict_json
from focusmate.core.skill_package import validate_skill_package
from tools.skill_evaluation import (PROMPT_VERSION, RUBRIC_VERSION, REVIEW_PROTOCOL_VERSION,
                                   REVIEW_PROMPT, apply_independent_review, digest, save_record, _validate_case)
from tools.skill_evaluation_cases import build_cases
from tools.skill_reviewer_evaluation import evaluate_fixed_answer, validate_fixture_audit


def main(argv=None):
    parser = argparse.ArgumentParser(description='固定回答审核器评测；不调用分析模型，不修改Skill。')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--case-audit', type=Path, required=True)
    parser.add_argument('--fixture-audit', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        fixtures = strict_json((ROOT / 'tools/fixtures/skill-reviewer-cases.json').read_text(encoding='utf-8'))
        audit = strict_json(args.fixture_audit.read_text(encoding='utf-8'))
        validate_fixture_audit(fixtures, audit)
        case_audit = strict_json(args.case_audit.read_text(encoding='utf-8'))
        source_hash = hashlib.sha256((ROOT / 'tools/skill_evaluation_cases.py').read_text(encoding='utf-8').encode()).hexdigest()
        cases = {case['id']: case for case in apply_independent_review(build_cases(), case_audit, source_hash)}
        reports = {}
        for fixture in fixtures:
            case = cases[fixture['baseCaseId']]
            _validate_case(case, ROOT / 'skills')
            reports[case['skillId']] = validate_skill_package(ROOT / 'skills' / case['skillId'])
        args.output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, KeyError, TypeError):
        print('未启动：材料/独立复核不合规，或输出目录已存在。', flush=True)
        return 1
    if args.env_file:
        load_local_env(args.env_file)
    try:
        provider = DeepSeekSkillEvaluationProvider.from_environment()
    except ProviderFailure:
        save_record(args.output, 'configuration-failure', {'status': 'unavailable'})
        return 1
    save_record(args.output, 'manifest', {
        'createdAt': datetime.now(timezone.utc).isoformat(), 'evaluationMode': 'reviewOnly',
        'fixturesHash': digest(fixtures), 'fixtures': fixtures, 'fixtureAudit': audit,
        'baseCasesHash': digest([cases[f['baseCaseId']] for f in fixtures]),
        'packages': reports, 'modelConfig': provider.public_config,
        'promptVersion': PROMPT_VERSION, 'rubricVersion': RUBRIC_VERSION,
        'reviewProtocolVersion': REVIEW_PROTOCOL_VERSION, 'reviewPrompt': REVIEW_PROMPT})
    counts = Counter()
    for fixture in fixtures:
        case = cases[fixture['baseCaseId']]
        if validate_skill_package(ROOT / 'skills' / case['skillId']) != reports[case['skillId']]:
            row = {'id': fixture['id'], 'goalContext': case['goalContext'], 'status': 'inputError',
                   'failureReason': 'SKILL_CHANGED_AFTER_RUN_FREEZE', 'calibrationStatus': 'incomplete'}
        else:
            row = evaluate_fixed_answer(fixture, case, ROOT / 'skills', provider)
        row['modelConfig'] = provider.public_config
        save_record(args.output, fixture['id'], row)
        counts[row['calibrationStatus']] += 1
        print(f"{fixture['id']}: {row['calibrationStatus']} (review={row['status']})", flush=True)
    passed = counts['passed'] == len(fixtures)
    save_record(args.output, 'summary', {'caseCount': len(fixtures), 'counts': dict(counts),
               'completed': True, 'allPassed': passed, 'scope': '仅四份固定回答审核，不证明一般审核能力或用户Goal完成。'})
    return 0 if passed else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
