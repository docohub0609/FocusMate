"""Run the frozen T062 evaluation. Development and holdout runs use separate output dirs."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from tools.stage_local_demo import create_case_runner
from tools.stage_local_evaluation import load_cases, run_evaluation


def main():
    from apps.api.deepseek_provider import load_local_env
    from focusmate.adapters.stage_recognition import DeepSeekStageRecognitionProvider
    from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--group", choices=("development", "holdout", "regression", "new", "all"), required=True)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; evaluation history must not be overwritten")
    if args.env_file:
        load_local_env(args.env_file)
    cases = load_cases(args.cases)
    modes = {case['input']['mode'] for case in cases if args.group == 'all' or case['group'] == args.group}
    stage = DeepSeekStageRecognitionProvider.from_environment() if modes & {'stage_only', 'combined'} else None
    dialogue = DeepSeekToolChatProvider.from_environment() if modes & {'assist_only', 'combined'} else None
    config = {name: provider.public_config for name, provider in [('stage', stage), ('dialogue', dialogue)] if provider}
    report = run_evaluation(cases, create_case_runner(stage, dialogue),
                            args.output, config, args.group)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
