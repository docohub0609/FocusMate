"""Run one frozen development or held-out T053 phase."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from apps.api.deepseek_provider import ProviderFailure, load_local_env
from focusmate.adapters.skill_evaluation import DeepSeekSkillEvaluationProvider
from tools.skill_routing_cases import get_cases
from tools.skill_routing_experiment import run


def main():
    parser = argparse.ArgumentParser(description='T053三组独立对照；记录生成，不宣称内容通过。')
    parser.add_argument('--phase', choices=['development', 'holdout'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--version', type=int, default=7)
    args = parser.parse_args()
    if args.output.exists():
        print('结果目录已存在，不覆盖。')
        return 1
    try:
        audit = json.loads(args.audit.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        print('缺少有效的案例独立核对记录，未调用模型。')
        return 1
    if args.env_file:
        load_local_env(args.env_file)
    try:
        provider = DeepSeekSkillEvaluationProvider.from_environment()
    except ProviderFailure:
        provider = None
    result = run(get_cases(args.phase), ROOT / 'skills/tree-and-graph', args.version,
                 provider, args.output, audit)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('counts', {}).get('generated') == 48 else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
