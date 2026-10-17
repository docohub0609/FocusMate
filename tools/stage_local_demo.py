"""Per-record T062 runner using the real stage entry and Docker-backed local tool."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.stage_recognition import recognize_stage, render_stage_report
from focusmate.core.code_materials import resolve_code_materials, code_material_payload, CodeMaterialError
from focusmate.contracts.tool_calls import validate_context
from focusmate.core.tool_dialogue import run_tool_dialogue
from focusmate.core.tool_gateway import ToolGateway
from services.oj_worker.local_runs import LocalRunService

LOCAL_GUIDANCE_VERSION = 't062-assistance-v6'
LOCAL_GUIDANCE = """只处理用户当前问题。解释某行就解释该行；运行某段就报告实际结果及必要含义，不回顾整题目标、不强制追加下一步。
所有材料都按编号和版本理解。Goal和题目是要求；用户声明、源码、真实工具结果各有来源，不能混成已验证事实。旧记录不证明新代码。
要说某行会报错，先确认提供的输入确实进入外层循环/分支并执行那行。描述边界更新时准确说哪个变量变了，不用“两端各移动一格”等不符合代码的泛化。
执行请求分三种：用户明确不要运行则直接解释；用户明确请求（包括“能不能先跑一下”），且许可、输入和依赖齐备时直接申请工具，不重复询问是否执行；其他情况只有运行能解决具体疑点才申请。
检查真实工具能力：局部工具不带函数外的import、其他函数或全局对象，JSON不能提供模块；不支持内部逐步变量追踪。发现这种明确不支持的依赖，说明本次不能这样执行就结束，不能承诺“给参数后再跑”、不能建议把不支持的import选进去试试。
运行后用“实际计算得到”描述数值；是否符合题意要另作对照。没有证明时不能称最优值或正确答案。发现结果违背题目约束，应指出具体冲突，不能一边称正确一边承认违反约束。
只解释本次选区和初值，不把它等同完整程序。不要补用户尚未写出的完整递推公式或算法。不要为了凑结构分别重述用户、代码、题目三大段。
progressOpinion（若提供）只是模型的阶段意见，程序已单独展示。本次不要重新确定整体阶段；缺完整题意时仍可按明确初值解释或运行独立片段，但不能猜整体缺哪些核心步骤。
"""



def _save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def _provision(path, case_input, records):
    goal = case_input["goalContext"]["goalId"]
    problem = case_input["problemRef"]
    code = next(record for record in records if record["ref"] == case_input["stageRequest"]["codeSnapshotRef"])
    source = code["content"]["text"]
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE goal_records(goal_id TEXT PRIMARY KEY,problem_id TEXT,problem_version INTEGER);
        CREATE TABLE code_snapshots(code_snapshot_id TEXT PRIMARY KEY,goal_id TEXT,code_version INTEGER,source_code TEXT,content_hash TEXT);
        CREATE TABLE submissions(submission_id TEXT PRIMARY KEY,goal_id TEXT,problem_id TEXT,problem_version INTEGER,code_version INTEGER,status TEXT);""")
        db.execute("INSERT INTO goal_records VALUES(?,?,?)",
                   (goal, problem["problemId"], problem["problemVersion"]))
        db.execute("INSERT INTO code_snapshots VALUES(?,?,?,?,?)",
                   (code["ref"], goal, case_input["codeVersion"], source,
                    hashlib.sha256(source.encode()).hexdigest()))


MODES = {'stage_only', 'assist_only', 'combined'}


class _CountedProvider:
    def __init__(self, provider):
        self.provider = provider
        self.public_config = deepcopy(getattr(provider, 'public_config', {}))
        self.requests = 0

    def complete(self, **kwargs):
        self.requests += 1
        return self.provider.complete(**kwargs)


def create_case_runner(stage_provider=None, dialogue_provider=None, *, local_runner=None):
    """Explicit developer tasks. No semantic router and no production authorization."""
    def run(case_input, directory):
        directory = Path(directory)
        case_input = deepcopy(case_input)
        mode = case_input.get('mode')
        try:
            if mode not in MODES:
                raise CodeMaterialError('INVALID_TASK_MODE')
            request = case_input['stageRequest']
            records = resolve_code_materials(request, MapContextResolver(case_input['records']))
            if case_input['goalContext'] != request['goalContext']:
                raise CodeMaterialError('SOURCE_SCOPE_MISMATCH')
            current = next(r for r in records if r['ref'] == request['codeSnapshotRef'])
            if (type(case_input['codeVersion']) is not int or
                    case_input['codeVersion'] != current['version']):
                raise CodeMaterialError('SOURCE_VERSION_MISMATCH')
            if type(case_input['localRunAllowed']) is not bool:
                raise CodeMaterialError('INVALID_PERMISSION')
            context = {'loopId': 'loop-' + uuid4().hex,
                       'goalContext': deepcopy(request['goalContext']),
                       'problemRef': deepcopy(case_input['problemRef']),
                       'allowedCodeVersions': [case_input['codeVersion']],
                       'allowedTools': ['run_local_code'] if case_input['localRunAllowed'] else [],
                       'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}
            validate_context(context)
            materials = code_material_payload(request, records)
        except CodeMaterialError as error:
            return {'status': 'input_failed', 'failure': {'reason': error.reason}}
        except Exception:
            return {'status': 'input_failed', 'failure': {'reason': 'INVALID_INPUT'}}
        _save(directory / 'materials.json', {'goalContext': request['goalContext'],
                                             'currentCodeRef': request['codeSnapshotRef'],
                                             'records': records})
        stage_report = None
        dialogue = None
        display = None
        stage_calls, dialogue_calls = _CountedProvider(stage_provider), _CountedProvider(dialogue_provider)
        if mode in ('stage_only', 'combined'):
            stage_audit = []
            stage_report = recognize_stage(request, MapContextResolver(records), stage_calls,
                audit_sink=lambda row: stage_audit.append(deepcopy(row)))
            _save(directory / 'stage-audit.json', stage_audit)
            _save(directory / 'stage-report.json', stage_report)
            display = render_stage_report(stage_report)
            _save(directory / 'stage-display.json', {'text': display})
        if mode in ('assist_only', 'combined'):
            # The dialogue budget starts now, independently of the preceding stage call.
            context['deadlineAt'] = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
            database = directory / 'fixture.sqlite3'
            _provision(database, case_input, records)
            local_service = LocalRunService(directory / 'local-work', runner=local_runner,
                                            task_database_path=directory / 'local-tasks.sqlite3')
            backend = SQLiteToolBackend(database, local_service=local_service)
            gateway = ToolGateway(backend, SQLiteToolLedger(directory / 'tool-calls.sqlite3'))
            materials['instruction'] = LOCAL_GUIDANCE
            if stage_report and stage_report['status'] == 'recognized':
                materials['progressOpinion'] = deepcopy(stage_report['result'])
            dialogue = run_tool_dialogue(context, materials, dialogue_calls, gateway,
                                         tools=TOOL_DEFINITIONS, budget=request['budget'])
            _save(directory / 'dialogue-report.json', dialogue)
        outcomes = ([stage_report['status'] == 'recognized'] if stage_report else []) + (
                    [dialogue['status'] == 'answered'] if dialogue else [])
        status = 'completed' if all(outcomes) else 'partial' if any(outcomes) else 'failed'
        return {'status': status, 'mode': mode, 'stageReport': stage_report,
                'dialogueReport': dialogue, 'display': display,
                'modelRequests': {'stage': stage_calls.requests, 'assistance': dialogue_calls.requests},
                'executionSource': ('injected-runner' if local_runner else 'docker')
                    if dialogue and any('rawOutput' in row.get('data', {})
                                        for row in dialogue['toolResults']) else 'no_returned_execution_evidence',
                'authorizationSource': 'developer-evaluation-fixture'}
    return run
