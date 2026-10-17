"""Run fixed T057 developer scenarios; no automatic semantic acceptance."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'src')]
from tools.tool_evaluation import load_cases, run_evaluation


def main():
    from apps.api.deepseek_provider import load_local_env
    from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
    from tools.tool_demo import create_fixture_runner
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--cases', type=Path)
    parser.add_argument('--group', choices=['development', 'holdout', 'all'], required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists; history must not be overwritten')
    if args.env_file:
        load_local_env(args.env_file)
    provider = DeepSeekToolChatProvider.from_environment()
    report = run_evaluation(load_cases(args.cases), create_fixture_runner(provider), args.output,
                            provider.public_config, args.group)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
