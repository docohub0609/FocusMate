"""Run the fixed T063 development or new-scene set; never reroll failed answers."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.progress_estimation import DeepSeekProgressEstimationProvider
from progress_evaluation import load_cases,run_evaluation


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--group',choices=['development','holdout'],required=True)
    p.add_argument('--cases',nargs='*')
    p.add_argument('--env-file',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--workers',type=int,default=3,choices=[1,2,3,4])
    args=p.parse_args()
    if args.output_dir.exists():p.error('output directory exists')
    cases=[c for c in load_cases() if c['group']==args.group and (not args.cases or c['id'] in args.cases)]
    if not cases:p.error('no selected cases')
    load_local_env(args.env_file)
    provider=DeepSeekProgressEstimationProvider.from_environment()
    run_evaluation(cases,provider,args.output_dir,workers=args.workers)


if __name__=='__main__':main()
