"""Independent Coach worker, including provider-free final OJ verification."""
import argparse
import json
import os
from pathlib import Path
import time
from apps.api.coach_service import CoachService
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.coach_chat import DeepSeekCoachProvider
from .worker import CoachWorker


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=os.getenv('FOCUSMATE_DB_PATH','data/focusmate.db'))
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--poll-seconds',type=float,default=2)
    args=parser.parse_args()
    if args.poll_seconds<=0:parser.error('poll-seconds must be positive')
    load_local_env(args.env_file)
    worker=CoachWorker(CoachService(args.db),provider_factory=DeepSeekCoachProvider.from_environment)
    print(json.dumps({'service':'focusmate-coach-worker','status':'polling'}),flush=True)
    try:
        while True:
            try:step=worker.process_once()
            except Exception:
                print(json.dumps({'service':'focusmate-coach-worker','status':'unavailable'}),flush=True)
                if args.once:return 1
                time.sleep(args.poll_seconds);continue
            if args.once:return 0
            if step is None:time.sleep(args.poll_seconds)
    except KeyboardInterrupt:return 0


if __name__=='__main__':raise SystemExit(main())
