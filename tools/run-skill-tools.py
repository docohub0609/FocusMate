"""Local, explicitly authorized Skill + tool demo; no production session or Goal writes."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.skill_execution import DeepSeekSkillExecutionProvider
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.tool_gateway import ToolGateway
from focusmate.core.skill_tool_execution import execute_skill_with_tools
from focusmate.contracts.skill_files import strict_json
from services.oj_worker.tool_public_runs import PublicRunService
from services.oj_worker.local_runs import LocalRunService


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--env-file',type=Path)
    p.add_argument('--enable-tools',action='store_true')
    a=p.parse_args(argv)
    a.output.mkdir(parents=True,exist_ok=False)
    value=strict_json(a.input.read_text(encoding='utf-8-sig'))
    class Lazy:
        def __init__(self,factory):self.factory=factory;self.instance=None
        @property
        def public_config(self):
            return dict(self.instance.public_config) if self.instance is not None else {}
        def complete(self,**kwargs):
            if self.instance is None:
                if a.env_file:load_local_env(a.env_file)
                self.instance=self.factory()
            return self.instance.complete(**kwargs)
    service=PublicRunService(a.database,a.state_dir/'public-runs')
    service.recover()
    local=LocalRunService(a.state_dir/'local-runs');local.recover()
    gateway=ToolGateway(SQLiteToolBackend(a.database,service,local),SQLiteToolLedger(a.state_dir/'calls.sqlite3'))
    analyst=Lazy(DeepSeekToolChatProvider.from_environment)
    reviewer=Lazy(DeepSeekSkillExecutionProvider.from_environment)
    report=execute_skill_with_tools(value['request'],value['context'],
        analyst,reviewer,
        gateway,enabled=a.enable_tools)
    report['providerConfigs']={'analysis':analyst.public_config,'review':reviewer.public_config}
    if 'toolDialogue' in report:
        report['toolDialogue']['modelConfig']=analyst.public_config
    report['authorizationSource']='developer-input-not-production-login'
    (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(report['status'])
    return 0 if report['status']=='accepted' else 1


if __name__=='__main__':raise SystemExit(main())
