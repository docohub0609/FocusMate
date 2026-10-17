"""Check saved trusted OJ evidence; no model, runner or production authorization."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'src')]
from focusmate.adapters.final_oj import SQLiteFinalOJStore
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.contracts.final_oj import FinalOJError
from focusmate.contracts.manager import ManagerError
from focusmate.contracts.skill_files import strict_json
from focusmate.core.final_oj import GoalStateManager, checked_request
from focusmate.core.session_manager import SessionManager


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--request',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--operation',choices=('verify','query','state'),default='verify')
    parser.add_argument('--goal-id')
    parser.add_argument('--manager-db',type=Path)
    parser.add_argument('--session-id')
    parser.add_argument('--watch',action='store_true')
    args = parser.parse_args(argv)
    try: args.output_dir.mkdir(parents=True,exist_ok=False)
    except FileExistsError:
        print('结果目录已存在，未覆盖。'); return 1
    report = {'status':'requestFailed','reason':'INPUT_OR_STORAGE_ERROR'}
    try:
        verifier = GoalStateManager(SQLiteFinalOJStore(args.db))
        if args.operation == 'state':
            if not args.goal_id: raise FinalOJError('GOAL_ID_REQUIRED')
            state = verifier.goal_state(args.goal_id)
            report = {'status':'found' if state else 'notInitialized','goalState':state}
        else:
            if not args.request: raise FinalOJError('REQUEST_REQUIRED')
            req = checked_request(strict_json(args.request.read_text(encoding='utf-8-sig')))
            (args.output_dir/'input.json').write_text(json.dumps(req,ensure_ascii=False,indent=2),encoding='utf-8')
            manager = None
            if args.manager_db or args.session_id:
                if not args.manager_db or not args.session_id or not args.manager_db.is_file():
                    raise FinalOJError('EXISTING_MANAGER_SESSION_REQUIRED')
                manager = SessionManager(SQLiteManagerStore(args.manager_db))
            if args.operation == 'query':
                report = verifier.report(req['goalId'],req['requestId']) or {'status':'notFound'}
            else:
                receipt = manager.start_final_verification(args.session_id,req,verifier) if manager else None
                while True:
                    report = (manager.advance_final_verification(args.session_id,verifier,round_id=receipt['roundId'])
                              if manager else verifier.verify(req))
                    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                    if not args.watch or report['status'] != 'waitingResult': break
                    time.sleep(1)
                if manager:
                    (args.output_dir/'manager-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    except KeyboardInterrupt:
        report = {**report,'watchStopped':True}
    except Exception as exc:
        report = {'status':'requestFailed','reason':str(exc) if isinstance(exc,(FinalOJError,ManagerError)) else 'INPUT_OR_STORAGE_ERROR'}
    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'reason':report.get('reason')},ensure_ascii=False))
    return 1 if report['status'] in ('requestFailed','superseded','expired','cancelled','notFound') else 0


if __name__ == '__main__': raise SystemExit(main())
