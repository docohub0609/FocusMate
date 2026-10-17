"""Explicit simulated tool-result faults; never real Docker evidence."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.tool_gateway import ToolGateway
from focusmate.core.tool_dialogue import run_tool_dialogue


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--env-file',type=Path,required=True)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    load_local_env(args.env_file)
    provider=DeepSeekToolChatProvider.from_environment()
    for kind in ('version_mismatch','content_conflict'):
        ctx={'loopId':kind,'goalContext':{'goalId':'demo','scope':'subgoal','subgoalId':'check-output'},
             'problemRef':{'problemId':'sum','problemVersion':1},'allowedCodeVersions':[2],
             'allowedTools':['run_public_samples'],
             'deadlineAt':(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()}
        materials={'userRequest':{'ref':'q1','text':'请运行版本2的公开样例，告诉我结果是否与这段代码一致。'},
            'problemFacts':[{'ref':'p1','text':'本题没有输入，要求输出2与3的和，公开样例期望输出5。'}],
            'codeExcerpt':{'ref':'c1','codeVersion':2,'startLine':1,'source':'print(2 + 3)'},
            'currentCodeVersion':2}
        class SimulatedBackend:
            def load_code(self,context,version):
                return {'codeVersion':2,'sourceCode':'print(2 + 3)',
                        'contentHash':hashlib.sha256(b'print(2 + 3)').hexdigest()}
            def run_public(self,context,code):
                return {'status':'succeeded','sampleStatus':'failed','goalContext':context['goalContext'],
                    'problemRef':context['problemRef'],'codeVersion':1 if kind=='version_mismatch' else 2,
                    'caseResults':[{'outcome':'outputMismatch','input':'','expectedOutput':'5',
                        'actualOutput':'0','error':'','inputTruncated':False,'expectedOutputTruncated':False,
                        'actualOutputTruncated':False,'errorTruncated':False}]}
        start=time.monotonic()
        report=run_tool_dialogue(ctx,materials,provider,ToolGateway(SimulatedBackend(),SQLiteToolLedger(args.output/(kind+'.sqlite3'))),tools=TOOL_DEFINITIONS)
        record={'simulation':True,'kind':kind,'notRealExecution':True,'materials':materials,
                'report':report,'durationSeconds':time.monotonic()-start,'reviewStatus':'pending'}
        (args.output/(kind+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(kind,report['status'],flush=True)


if __name__=='__main__':main()
