"""Development assembly. Core owns decisions; adapters own persistence and I/O."""
from pathlib import Path
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.core.session_manager import SessionManager
from focusmate.core.manager_execution import advance_once
from focusmate.core.diagnosis_execution import DiagnosisExecutor
from focusmate.core.skill_sections import load_index

ROOT=Path(__file__).resolve().parents[1]


def enabled_skills(configuration):
    return [load_index(ROOT/p['path'],p['version'],p['hash']) for p in configuration]


def run_diagnosis(bundle,provider,directory,*,skills,gateway=None,backend=None,lookup=None,plan_action=False,diagnosis=None,action_executor=None,review_provider=None):
    """Persist every step before continuing. A reopened runner does not reissue models."""
    req=bundle['request'];manager=SessionManager(SQLiteManagerStore(Path(directory)/'manager.sqlite3'))
    session='coaching' if plan_action else 'diagnosis';records={r['ref']:r for r in bundle['records']}
    code=records.get(req.get('currentCodeRef'))
    try:manager.read(session)
    except Exception:
        manager.create(session,req['goalContext'],req['problemRef'],contract_version=req['contractVersion'],
            code_version=code['version'] if code else None,allowed_tools=list(getattr(gateway,'diagnosis_allowed_tools',[])),mode=session)
        manager.command(session,'initial-request','ask',{'text':records[req['userRequestRef']]['content']['text']})
    if plan_action:
        from focusmate.core.action_execution import ActionExecutor
        executor=(action_executor or ActionExecutor)(provider,gateway,backend,bundle=bundle,skills=skills,diagnosis=diagnosis,review_provider=review_provider)
    else:executor=DiagnosisExecutor(provider,gateway,backend,bundle=bundle,skills=skills)
    while True:
        tick=advance_once(manager,session,executor,lookup=lookup)
        if tick['status'] in ('idle','not_applied'):break
    if plan_action:manager.publish(session)
    state=manager.read(session)
    return state
