"""Run a T054 frozen baseline, ablation, or held-out comparison."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.skill_evaluation import DeepSeekSkillEvaluationProvider
from tools.skill_improvement_cases import get_cases
from tools.skill_improvement_experiment import capture_packages, run


def main():
    parser = argparse.ArgumentParser(description='T054真实对照；不自动修正原始回答。')
    parser.add_argument('--phase', required=True, choices=['development', 'holdout'])
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--arms', nargs='+', default=['ordinary', 'full', 'selective'])
    parser.add_argument('--case-id', action='append')
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('结果目录已存在，不覆盖。')
    cases = get_cases(args.phase)
    if args.case_id:
        requested = set(args.case_id)
        cases = [c for c in cases if c['id'] in requested]
        if {c['id'] for c in cases} != requested:
            parser.error('案例编号不存在。')
    audit = json.loads(args.audit.read_text(encoding='utf-8'))
    packages = ({k: v['files'] for k, v in json.loads(args.baseline.read_text(encoding='utf-8')).items()}
                if args.baseline else capture_packages(ROOT / 'skills'))
    if args.env_file:
        load_local_env(args.env_file)
    provider = DeepSeekSkillEvaluationProvider.from_environment()
    summary = run(cases, packages, provider, args.output, audit, arms=tuple(args.arms), workers=args.workers)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary.get('counts', {}).get('generated') == len(cases) * len(args.arms) * 2 else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
