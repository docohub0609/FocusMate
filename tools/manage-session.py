"""Developer session commands and single-step worker; not production authorization."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from focusmate.contracts.manager import ManagerError, safe_payload
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.adapters.manager_runs import ManagerRunLookup
from focusmate.core.session_manager import SessionManager
from focusmate.core.manager_execution import ManagerExecutor, advance_once, reconcile_runs
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.core.tool_gateway import ToolGateway
from apps.api.deepseek_provider import load_local_env
from services.oj_worker.tool_public_runs import PublicRunService
from services.oj_worker.local_runs import LocalRunService


class FixtureProvider:
    public_config={'provider':'Fake','maxTokens':256,'source':'developerFixture'}
    def __init__(self,response):self.response=response
    def complete(self,**kwargs):return self.response


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manager-db',type=Path,required=True);p.add_argument('--input',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--database',type=Path)
    p.add_argument('--state-dir',type=Path);p.add_argument('--env-file',type=Path)
    p.add_argument('--fixture-response',type=Path);p.add_argument('--fixture-roles',action='store_true')
    p.add_argument('--watch',action='store_true',help='Process until Ctrl-C; saves state without ending pending work.')
    a=p.parse_args()
    if a.output_dir.exists():p.error('output directory already exists')
    a.output_dir.mkdir(parents=True)
    report={'schemaVersion':'1.0.0','source':'developer-input-not-production-authorization','status':'failed'}
    start=time.monotonic()
    try:
        value=json.loads(a.input.read_text(encoding='utf-8-sig'));safe_payload(value)
        operation=value['operation'];session=value['sessionId']
        if operation!='create' and not a.manager_db.is_file():raise ManagerError('MANAGER_DATABASE_MISSING')
        m=SessionManager(SQLiteManagerStore(a.manager_db))
        if operation=='create':
            report['result']=m.create(session,value['goalContext'],value['problemRef'],contract_version=value['contractVersion'],
                code_version=value['codeVersion'],allowed_tools=value['allowedTools'],limits=value.get('limits'))
        elif operation=='command':report['result']=m.command(session,value['requestId'],value['kind'],value['payload'])
        elif operation=='query':report['result']=m.read(session)
        elif operation=='publish':report['result']=m.publish(session)
        elif operation in ('advance','recover'):
            if a.database is None or not a.database.is_file() or a.state_dir is None:raise ManagerError('BUSINESS_DATABASE_AND_STATE_REQUIRED')
            public=PublicRunService(a.database,a.state_dir/'public')
            local=LocalRunService(a.state_dir/'local')
            backend=SQLiteToolBackend(a.database,public,local)
            lookup=ManagerRunLookup(public,local)
            if operation=='recover':
                report['toolRecovery']=reconcile_runs(m,session,lookup)
                report['result']=m.recover(session)
                reconcile_runs(m,session,lookup)
            else:
                if a.fixture_roles and not a.fixture_response:raise ManagerError('FIXTURE_ROLES_REQUIRE_FAKE_PROVIDER')
                if a.fixture_response:
                    response=json.loads(a.fixture_response.read_text(encoding='utf-8-sig'));safe_payload(response)
                    provider=FixtureProvider(response)
                else:
                    if a.env_file:load_local_env(a.env_file)
                    provider=DeepSeekToolChatProvider.from_environment()
                e=ManagerExecutor(provider,ToolGateway(backend,SQLiteToolLedger(a.state_dir/'tool-calls.sqlite3')),backend,
                                  fixture_roles=a.fixture_roles,materials=value.get('materials'),budget=value.get('budget'))
                history=[]
                last_revision=-1
                try:
                    while True:
                        item=advance_once(m,session,e,lookup=lookup)
                        if item['status']!='idle':history.append(item)
                        if a.watch:m.publish(session)
                        state=m.read(session)
                        if state['revision']!=last_revision:
                            save(a.output_dir/'report.json',{**report,'status':'running','steps':history,'state':state})
                            last_revision=state['revision']
                        if not a.watch:break
                        time.sleep(.25)
                except KeyboardInterrupt:report['interruptedByOperator']=True
                report['result']=history
            report['state']=m.read(session)
        else:raise ManagerError('UNKNOWN_OPERATION')
        report['status']='processed';save(a.output_dir/'input.json',value)
    except Exception as exc:
        report['failure']=str(exc) if isinstance(exc,ManagerError) else 'INPUT_OR_STORAGE_ERROR'
    report['durationSeconds']=time.monotonic()-start
    save(a.output_dir/'report.json',report)
    print(json.dumps({'status':report['status'],'failure':report.get('failure')},ensure_ascii=False))
    return 0 if report['status']=='processed' else 1


if __name__=='__main__':raise SystemExit(main())
