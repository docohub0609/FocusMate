"""Opt-in pinned Skill analysis with native tools and the existing independent reviewer."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import time

from focusmate.contracts.skill_files import strict_json
from focusmate.contracts.tool_calls import TOOL_DEFINITIONS, TOOL_PARAMETERS, validate_context
from focusmate.core.skill_execution import _prepare, _run, ACTOR_PROMPT, REVIEW_PROMPT
from focusmate.core.skill_registry import DEFAULT_ROOT
from focusmate.core.tool_dialogue import run_tool_dialogue, TOOL_PROMPT


SKILL_TOOL_PROMPT = ACTOR_PROMPT.replace('返回符合 outputSchema 的 JSON，仅有 applicability/findings/unknowns', '需要取证时发原生 tool_calls；取证完成后的最终回答才返回符合 outputSchema 的 JSON，仅有 applicability/findings/unknowns').replace('不执行代码或工具，不写入 Goal', '只能通过本轮允许的工具获取材料，不写入 Goal') + '\n' + TOOL_PROMPT + '''
最终回答必须符合 materials.outputSchema，保持 applicability/findings/unknowns。
可引用原始材料的 ref/snapshotRef，以及真实工具返回的 ref；不得自己构造引用。
用户明确要求读取或运行且本轮已许可时，应取得对应工具证据再回答；不能只做静态分析并把可获取材料写成未知。工具需求不授予权限。工具失败只说明证据缺口。公开样例结果仅限指定代码版本与这些样例。'''


def execute_skill_with_tools(request, context, tool_provider, review_provider, gateway, *, enabled=False,
                             skills_root=DEFAULT_ROOT, deadline_seconds=120, clock=time.monotonic,
                             max_model_requests=7, max_tool_requests=3, budget=None):
    start=clock()
    counts={'analysis':0,'tools':0,'review':0,'correction':0,'modelTotal':0}
    report={'schemaVersion':'1.0.0','status':'rejected','counts':counts}
    def reject(stage,reason):
        report['failure']={'stage':stage,'reason':reason}
        return report
    try:
        if enabled is not True or 'sections' not in request:
            raise ValueError('explicit opt-in and sections required')
        if type(deadline_seconds) not in (int,float) or not 0 < deadline_seconds <= 120:
            raise ValueError('deadline')
        if type(max_model_requests) is not int or not 4 <= max_model_requests <= 7:
            raise ValueError('model budget')
        if type(max_tool_requests) is not int or not 0 <= max_tool_requests <= 3:
            raise ValueError('tool budget')
        ctx=deepcopy(context);validate_context(ctx)
        if ctx['goalContext'] != request['goalContext']:
            raise ValueError('goal context')
        payload,schema,refs=_prepare(deepcopy(request),skills_root,allow_tools=True)
        if not set(payload['requiredTools']) <= set(TOOL_PARAMETERS):
            raise ValueError('unsupported required tool')
        now=datetime.now(timezone.utc)
        remaining=min(deadline_seconds,(datetime.fromisoformat(ctx['deadlineAt'])-now).total_seconds())
        deadline=start+remaining
        if remaining <= 0: raise ValueError('expired context')
        ctx['deadlineAt']=(now+timedelta(seconds=remaining)).isoformat()
    except Exception:
        return reject('input','INVALID_INPUT')
    report['goalContext']=deepcopy(request['goalContext'])
    dialogue=run_tool_dialogue(ctx,payload,tool_provider,gateway,tools=TOOL_DEFINITIONS,budget=budget,
        clock=clock,max_model_requests=max_model_requests-3,max_tool_requests=max_tool_requests,
        duration_seconds=remaining,system_prompt=SKILL_TOOL_PROMPT,absolute_deadline=deadline)
    report['toolDialogue']=dialogue
    counts.update(analysis=dialogue['modelRequests'],tools=dialogue['toolRequests'],modelTotal=dialogue['modelRequests'])
    if dialogue['status']!='answered':
        return reject('candidate',dialogue['failure']['code'])
    try:
        candidate=strict_json(dialogue['answer'])
    except Exception:
        return reject('candidate','INVALID_CANDIDATE')
    try:
        results=deepcopy(dialogue['toolResults'])
        for result in results:
            ref=result.get('ref')
            if not isinstance(ref,str) or not ref.strip() or ref in refs:
                raise ValueError('tool reference mismatch')
            refs.add(ref)
        payload['toolResults']=results
        payload['toolContext']=ctx
    except Exception:
        return reject('candidate','INVALID_TOOL_EVIDENCE')
    # Keep the full supplied dialogue in this bounded bridge: no hidden summarizer calls.
    execution=_run(request,review_provider,skills_root=skills_root,deadline_seconds=deadline_seconds,
        clock=clock,audit_sink=None,prepared=(payload,schema,refs),initial_candidate=candidate,
        absolute_deadline=deadline,review_prompt=REVIEW_PROMPT+'\n若用户明确要求执行本轮已许可的工具，必须核对 toolResults 是否实际尝试了对应操作；未尝试而仅静态分析或声称缺证据，overall 应 fail。工具失败应如实保留，不要求成功。',context_options={'recentMessageCount':max(1,len(request['dialogue']))})
    report['execution']=execution
    for call in execution['requests']:
        counts['correction' if call['stage']=='repair' else 'review']+=1
    counts['modelTotal']+=execution['logicalRequests']
    report['status']=execution['status']
    if 'failure' in execution: report['failure']=execution['failure']
    if 'invocation' in execution: report['invocation']=execution['invocation']
    return report
