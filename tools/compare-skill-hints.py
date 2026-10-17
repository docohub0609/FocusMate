"""Compare 48 fresh hint requests using the approved, fixed twelve scenarios."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import ProviderFailure, load_local_env
from focusmate.adapters.skill_evaluation import DeepSeekSkillEvaluationProvider
from tools.skill_evaluation import apply_independent_review
from tools.skill_evaluation_cases import build_cases
from tools.skill_hint_comparison import run_comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description='简单提示与完整 Skill：固定 12 例、两轮、共 48 次生成，导出独立盲评材料。')
    parser.add_argument('--output', type=Path, required=True, help='新的结果目录；不覆盖历史')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--case-audit', type=Path, required=True, help='Explicit reviewed case record; never bundled user history')
    args = parser.parse_args(argv)
    if args.output.exists():
        print('结果目录已存在，未发送请求。', flush=True)
        return 1
    try:
        audit = json.loads(args.case_audit.read_text(encoding='utf-8'))
        source_hash = hashlib.sha256((ROOT / 'tools/skill_evaluation_cases.py').read_text(encoding='utf-8').encode()).hexdigest()
        cases = apply_independent_review(build_cases(), audit, source_hash)
    except (OSError, ValueError, KeyError, TypeError):
        print('案例独立复核未完成或材料已变化，未发送请求。', flush=True)
        return 1
    if args.env_file:
        load_local_env(args.env_file)
    try:
        provider = DeepSeekSkillEvaluationProvider.from_environment()
    except ProviderFailure:
        provider = None
    try:
        summary = run_comparison(cases, ROOT / 'skills', provider, args.output)
    except OSError:
        print('结果无法安全保存；已保存记录保留，未重试或覆盖。', flush=True)
        return 1
    if summary.get('status') == 'inputError':
        print('材料或 Skill 包预检失败，未发送请求。', flush=True)
        return 1
    print('生成记录与盲评材料已保存；generated 仅表示结构有效，不表示提示质量通过。', flush=True)
    return 0 if summary['counts'].get('generated') == 48 else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
