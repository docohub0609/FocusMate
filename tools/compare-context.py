"""Run the frozen T056 development or held-out context comparison."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.skill_execution import DeepSeekSkillExecutionProvider
from tools.context_evaluation import load_cases,run_experiment


def main(argv=None):
    parser=argparse.ArgumentParser(description='动态规划对话三组对照；只记录真实结果，不自动宣布内容验收通过。')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--group',choices=['development','holdout','all'],default='development')
    parser.add_argument('--arms',nargs='+',choices=['full','recent','organized'],default=['full','recent','organized'])
    args=parser.parse_args(argv)
    if args.output.exists():
        print('结果目录已存在，未发送请求。');return 1
    if args.env_file: load_local_env(args.env_file)
    try: provider=DeepSeekSkillExecutionProvider.from_environment()
    except Exception: provider=None
    result=run_experiment(load_cases(),provider,args.output,group=args.group,arms=args.arms)
    print(result,flush=True)
    return 0 if result['counts'].get('generated',0)==result['scheduledTargetCount'] else 1

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
