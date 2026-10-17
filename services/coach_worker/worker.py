"""One owned step per tick; final OJ verification does not require an AI provider."""
import threading
from uuid import uuid4
from jsonschema.exceptions import ValidationError

from focusmate.core.dynamic_coach_execution import DynamicCoachExecutor
from focusmate.contracts.manager import ManagerError


class CoachWorker:
    def __init__(self,service,provider=None,*,provider_factory=None,review_provider=None,repair_provider=None,gateway=None,lookup=None):
        self.service=service;self.provider=provider;self.provider_factory=provider_factory
        self.review_provider=review_provider;self.worker_id='coach-'+uuid4().hex
        self.repair_provider=repair_provider
        self.gateway=gateway;self.lookup=lookup
        self.failures=[]

    def tools(self):
        if self.gateway is None:
            from .tools import create_tools
            self.gateway,self.lookup=create_tools(self.service)

    def cleanup(self,session,state):
        for request in state['cancellations']:
            if request['status']!='pending':continue
            self.tools()
            result=self.lookup.cancel(state,state['steps'][request['stepId']])
            self.service.manager.record_cancellation(session,request['stepId'],result)

    def process_once(self):
        self.service.process_events()
        manager=self.service.manager
        for session in self.service.sessions():
            try:
                self.service.synchronize(session);manager.recover(session)
                state=manager.read(session);self.cleanup(session,state)
            except (ManagerError,ValueError) as exc:
                self.failures.append({'sessionId':session,'code':getattr(exc,'code',str(exc))[:120]})
                self.failures=self.failures[-20:]
                continue
            r=state['rounds'].get(state['currentRoundId'])
            if not r or r['status'] not in ('queued','processing') or state['status']=='paused':continue
            try:material=self.service.materials(session)
            except (ManagerError,ValueError) as exc:
                self.failures.append({'sessionId':session,'code':getattr(exc,'code',str(exc))[:120]})
                self.failures=self.failures[-20:]
                continue
            step=manager.claim(session,self.worker_id,materials=material)
            if not step:continue
            stop=threading.Event()
            def heartbeat():
                while not stop.wait(30):
                    try:manager.heartbeat(session,step['stepId'],step['token'])
                    except ManagerError:return
            keeper=threading.Thread(target=heartbeat,daemon=True);keeper.start()
            try:
                if step['kind']=='skill':
                    from focusmate.core.dynamic_coach_skills import read_skill
                    result=read_skill(step['payload'],step['input'].get('skillCatalog',[]))
                elif step['kind']=='tool':
                    from focusmate.core.tool_steps import execute_tool_step
                    self.tools()
                    checked=execute_tool_step(step['toolContext'],step['payload'],self.gateway)
                    result={'kind':'failure','code':checked['failure']} if 'failure' in checked else {'kind':'tool',**checked}
                else:
                    if self.provider is None and self.provider_factory:self.provider=self.provider_factory()
                    if self.provider is None:result={'kind':'failure','code':'MODEL_CONFIGURATION_REQUIRED'}
                    elif step['kind']=='repair':
                        from focusmate.core.dynamic_coach_repair import execute_repair
                        if self.repair_provider is None:
                            if hasattr(self.provider,'config'):
                                from focusmate.adapters.repair_agent import DeepSeekRepairProvider
                                self.repair_provider=DeepSeekRepairProvider(self.provider.config)
                            else:self.repair_provider=self.provider
                        result=execute_repair(manager,session,step,self.repair_provider,
                            review_provider=self.review_provider or self.provider)
                    else:result=DynamicCoachExecutor(self.provider,review_provider=self.review_provider).execute(manager.read(session),step)
                try:self.service.complete(session,step['stepId'],step['token'],result)
                except (ValueError,ValidationError,KeyError,TypeError) as exc:
                    if str(exc) in ('STALE_OWNER','COACH_BINDING_CHANGED'):return step['stepId']
                    code=str(exc) if str(exc).replace('_','').isalnum() else 'COACH_RESPONSE_REJECTED'
                    from focusmate.core.dynamic_coach_repair import correctable_exception
                    self.service.complete(session,step['stepId'],step['token'],{'kind':'failure','code':code,
                        'correctable':correctable_exception(exc),'rejectedResult':result,
                        'formatError':('.'.join(str(x) for x in exc.absolute_path)+': '+exc.message)[:500]
                            if isinstance(exc,ValidationError) else (
                                str(exc)+'; invalidEvidenceRefs='+repr(exc.invalid_refs)+
                                '; 这些编号不在当前evidenceCatalog中。旧对话/诊断里提及过不等于本次可引用；只选当前材料中实际支持结论的编号。'
                                if hasattr(exc,'invalid_refs') else
                                str(exc)+'; checkId='+exc.check_id+
                                '; 该项需要与evidenceType对应的当前证据。用户说已修改不能证明代码结构；缺证据请保留notReady，不要改写检查要求。'
                                if hasattr(exc,'check_id') else str(exc)[:200])})
            except Exception as exc:
                try:self.service.complete(session,step['stepId'],step['token'],{'kind':'failure','code':getattr(exc,'code','COACH_STEP_UNAVAILABLE'),'errorType':type(exc).__name__})
                except ManagerError:pass
            finally:
                stop.set();keeper.join(timeout=1)
            return step['stepId']
        return None
