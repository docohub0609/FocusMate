"""T071: diagnose a pinned request through the durable Manager; do not publish."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.progress_storage import SQLiteProgressResolver
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.tool_gateway import ToolGateway
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.diagnosis import prepare_diagnosis
from focusmate.contracts.skill_files import strict_json
from focusmate.contracts.schema_validation import validate
from diagnosis_runtime import enabled_skills,run_diagnosis


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def main(argv=None,*,plan_action=False):
    p=argparse.ArgumentParser(description='T072：生成并审核本次帮助，经Manager保存发布。' if plan_action else __doc__)
    p.add_argument('--request',type=Path,required=True);p.add_argument('--db',type=Path)
    p.add_argument('--public-task-db',type=Path);p.add_argument('--env-file',type=Path)
    p.add_argument('--developer-materials',action='store_true')
    p.add_argument('--skills',type=Path,default=ROOT/'skills/diagnosis-enabled.json')
    p.add_argument('--output-dir',type=Path,required=True)
    if plan_action:
        p.add_argument('--diagnosis',type=Path,help='已保存且与本次材料一致的诊断；省略时先执行T071。')
        p.add_argument('--action-profile',choices=['baseline','quality-candidate'],default='baseline',
                       help='quality-candidate为显式实验配置，需要8192输出预留；不会自动增加额度。')
    a=p.parse_args(argv)
    try:a.output_dir.mkdir(parents=True,exist_ok=False)
    except FileExistsError:print('结果目录已存在，没有覆盖。');return 1
    try:
        supplied=strict_json(a.request.read_text(encoding='utf-8-sig'));req=supplied['request']
        if a.developer_materials:
            bundle=prepare_diagnosis(req,MapContextResolver(supplied['records']))
            bundle['source']='developerFixture'
        else:
            if not a.db:raise ValueError('BUSINESS_DATABASE_REQUIRED')
            with SQLiteProgressResolver(a.db,req,public_task_db=a.public_task_db,supplemental_records=supplied.get('supplementalRecords',[])) as resolver:
                bundle=prepare_diagnosis(req,resolver)
            bundle['source']='businessReadOnlyWithExplicitSupplementalRecords'
        skills=enabled_skills(strict_json(a.skills.read_text(encoding='utf-8')))
        allowed=supplied.get('allowedTools',[])
        backend=gateway=lookup=None
        if allowed:
            if not a.db:raise ValueError('BUSINESS_DATABASE_REQUIRED_FOR_TOOLS')
            from services.oj_worker.tool_public_runs import PublicRunService
            from services.oj_worker.local_runs import LocalRunService
            from focusmate.adapters.manager_runs import ManagerRunLookup
            public=PublicRunService(a.db,a.output_dir/'public') if 'run_public_samples' in allowed else None
            local=LocalRunService(a.output_dir/'local') if 'run_local_code' in allowed else None
            backend=SQLiteToolBackend(a.db,public,local)
            gateway=ToolGateway(backend,SQLiteToolLedger(a.output_dir/'tool-calls.sqlite3'))
            gateway.diagnosis_allowed_tools=allowed
            lookup=ManagerRunLookup(public,local) if public or local else None
        if a.env_file:load_local_env(a.env_file)
        provider=DeepSeekToolChatProvider.from_environment()
        save(a.output_dir/'input.json',bundle);save(a.output_dir/'model-config.json',provider.public_config)
        diagnosis=strict_json(a.diagnosis.read_text(encoding='utf-8-sig')) if plan_action and a.diagnosis else None
        action_options={}
        if plan_action:
            from action_profiles import action_profile
            executor,reviewer,profile=action_profile(a.action_profile)
            action_options={'action_executor':executor,'review_provider':reviewer}
            save(a.output_dir/'action-profile.json',profile)
            planning_request={'schemaVersion':'1.0.0','diagnosisRequest':req,'diagnosisId':diagnosis['diagnosisId'] if diagnosis else None}
            validate('ActionPlanningRequest',planning_request);save(a.output_dir/'planning-request.json',planning_request)
        state=run_diagnosis(bundle,provider,a.output_dir,skills=skills,gateway=gateway,backend=backend,lookup=lookup,
                            plan_action=plan_action,diagnosis=diagnosis,**action_options)
        save(a.output_dir/'trace.json',state)
        result=state['rounds'][state['currentRoundId']]
        save(a.output_dir/'result.json',result)
        if plan_action:
            from focusmate.core.action_planning import planning_report
            save(a.output_dir/'planning-report.json',planning_report(state))
            print(state['outbox'][-1]['content'] if state['outbox'] else '本次帮助未生成完成，请稍后重试。')
            return 0 if state['outbox'] else 1
        print('诊断完成，行动规划未接入。' if 'diagnosis' in result else '诊断未完成：'+str(result.get('failure')))
        return 0 if 'diagnosis' in result else 1
    except Exception as exc:
        save(a.output_dir/'failure.json',{'status':'failed','code':str(exc) if isinstance(exc,ValueError) else 'INPUT_OR_SERVICE_UNAVAILABLE'})
        print('本次诊断未完成，原因已保存。');return 1


if __name__=='__main__':raise SystemExit(main())
