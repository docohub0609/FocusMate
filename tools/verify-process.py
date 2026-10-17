"""Verify one subgoal after an explicitly selected published Action."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.adapters.process_storage import SQLiteProcessResolver
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.adapters.manager_runs import ManagerRunLookup
from focusmate.contracts.skill_files import strict_json
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.process_verification import prepare_verification
from focusmate.core.process_execution import ProcessExecutor
from focusmate.core.session_manager import SessionManager
from focusmate.core.manager_execution import advance_once
from focusmate.core.tool_gateway import ToolGateway


def save(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('request','manager-db','output-dir'):parser.add_argument('--'+name,type=Path,required=True)
    for name in ('db','public-task-db','local-task-db','env-file'):parser.add_argument('--'+name,type=Path)
    parser.add_argument('--session-id',required=True);parser.add_argument('--request-id',required=True)
    parser.add_argument('--developer-materials',action='store_true')
    args=parser.parse_args(argv)
    try:args.output_dir.mkdir(parents=True,exist_ok=False)
    except FileExistsError:print('结果目录已存在，未覆盖。');return 1
    try:
        if not args.manager_db.is_file():raise ValueError('MANAGER_DATABASE_REQUIRED')
        manager=SessionManager(SQLiteManagerStore(args.manager_db));state=manager.read(args.session_id)
        supplied=strict_json(args.request.read_text(encoding='utf-8-sig'));req=supplied['request']
        action=next((a['learningAction'] for a in state['outbox'] if a['actionId']==req['actionId']),None)
        if action is None:raise ValueError('PUBLISHED_ACTION_REQUIRED')
        if args.developer_materials:
            bundle=prepare_verification(req,MapContextResolver(supplied['records']),action)
            bundle['origin']='developerFixture'
        else:
            if not args.db:raise ValueError('BUSINESS_DATABASE_REQUIRED')
            with SQLiteProcessResolver(args.db,req,public_task_db=args.public_task_db,local_task_db=args.local_task_db,
                    supplemental_records=supplied.get('supplementalRecords',[])) as resolver:
                bundle=prepare_verification(req,resolver,action)
            bundle['origin']='businessReadOnlyWithExplicitSupplementalRecords'
        if args.env_file:load_local_env(args.env_file)
        provider=DeepSeekToolChatProvider.from_environment()
        backend=gateway=lookup=None;allowed=state['allowedTools']
        if allowed:
            if not args.db:raise ValueError('BUSINESS_DATABASE_REQUIRED_FOR_TOOLS')
            from services.oj_worker.tool_public_runs import PublicRunService
            from services.oj_worker.local_runs import LocalRunService
            # Runtime tasks survive a new audit output directory on restart.
            runtime=args.manager_db.resolve().parent/(args.manager_db.stem+'-verification-runtime')
            runtime.mkdir(parents=True,exist_ok=True)
            public=PublicRunService(args.db,runtime/'public',task_database_path=args.public_task_db) if 'run_public_samples' in allowed else None
            local=LocalRunService(runtime/'local',task_database_path=args.local_task_db) if 'run_local_code' in allowed else None
            backend=SQLiteToolBackend(args.db,public,local);gateway=ToolGateway(backend,SQLiteToolLedger(runtime/'calls.sqlite3'))
            lookup=ManagerRunLookup(public,local)
        code=next((r for r in bundle['records'] if r['ref']==req['currentCodeRef']),None)
        receipt=manager.command(args.session_id,args.request_id,'verification',{'actionId':req['actionId'],
            'subgoalId':req['goalContext']['subgoalId'],'materialRefs':req['materialRefs'],'codeVersion':code['version'] if code else state['codeVersion']})
        save(args.output_dir/'input.json',bundle);save(args.output_dir/'model-config.json',provider.public_config)
        executor=ProcessExecutor(provider,gateway,backend,bundle=bundle)
        target=receipt['roundId']
        while True:
            state=manager.read(args.session_id);r=state['rounds'][target]
            if r['status'] in ('finished','failed','cancelled','superseded','expired','interrupted') or state['currentRoundId']!=target:break
            tick=advance_once(manager,args.session_id,executor,lookup=lookup)
            if tick['status'] in ('idle','not_applied'):break
        state=manager.read(args.session_id);r=state['rounds'][target]
        save(args.output_dir/'trace.json',state);save(args.output_dir/'result.json',r)
        print('验证结果已保存：'+r['verification']['verifierResult']['status'] if r.get('verificationAccepted') else '检查尚未完成，状态和原因已保存。')
        return 0 if r.get('verificationAccepted') else 1
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else getattr(exc,'code','INPUT_OR_SERVICE_UNAVAILABLE')
        save(args.output_dir/'failure.json',{'status':'requestFailed','code':code});print('检查未完成：'+code);return 1


if __name__=='__main__':raise SystemExit(main())
