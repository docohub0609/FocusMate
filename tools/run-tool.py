"""Explicit local tool demonstration; never a production authorization endpoint."""
import argparse,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.tool_gateway import ToolGateway
from focusmate.core.tool_dialogue import run_tool_dialogue
from services.oj_worker.tool_public_runs import PublicRunService
from services.oj_worker.local_runs import LocalRunService

def main():
    p=argparse.ArgumentParser(description='Local developer-authorized tool demonstration')
    p.add_argument('--input',type=Path,required=True);p.add_argument('--database',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--env-file',type=Path)
    a=p.parse_args()
    if a.output.exists():p.error('output directory already exists')
    value=json.loads(a.input.read_text(encoding='utf-8'))
    a.output.mkdir(parents=True)
    started=time.monotonic()
    service=PublicRunService(a.database,a.state_dir/'public-runs')
    service.recover()
    local=LocalRunService(a.state_dir/'local-runs');local.recover()
    gateway=ToolGateway(SQLiteToolBackend(a.database,service,local),SQLiteToolLedger(a.state_dir/'calls.sqlite3'))
    if MODE=='direct':report=gateway.invoke(value['context'],value['call'])
    else:
        if a.env_file:load_local_env(a.env_file)
        report=run_tool_dialogue(value['context'],value['materials'],DeepSeekToolChatProvider.from_environment(),gateway,tools=TOOL_DEFINITIONS)
    report['durationSeconds']=time.monotonic()-started
    report['authorizationSource']='developer-input-not-production-login'
    (a.output/'input.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(report['status'])
    return 0 if report['status'] in ('answered','succeeded') else 1

MODE='direct'
if __name__=='__main__':raise SystemExit(main())
