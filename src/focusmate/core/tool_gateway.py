"""Bounded tool gateway. Source access is injected; no business store imports."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
from focusmate.contracts.tool_calls import validate_context, validate_call, TOOL_DEFINITIONS
from focusmate.contracts.tool_ledger import ToolLedger


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

class ToolFailure(Exception):
    pass

class ToolGateway:
    def __init__(self, backend, ledger: ToolLedger, *, clock=lambda:datetime.now(timezone.utc)):
        self.backend=backend; self.ledger=ledger;self.clock=clock

    def invoke(self, context, call):
        ctx=deepcopy(context);call=deepcopy(call)
        result={'schemaVersion':'1.0.0','toolResultId':'tool-result-'+str(uuid4()),
            'callId':call.get('callId','invalid') if isinstance(call,dict) else 'invalid',
            'loopId':ctx.get('loopId','invalid') if isinstance(ctx,dict) else 'invalid',
            'goalContext':ctx.get('goalContext',{}) if isinstance(ctx,dict) else {},
            'problemRef':ctx.get('problemRef',{}) if isinstance(ctx,dict) else {},
            'toolName':call.get('toolName','invalid') if isinstance(call,dict) else 'invalid',
            'createdAt':self.clock().isoformat(),'status':'rejected'}
        result['ref']=result['toolResultId']
        record_id=None
        try:
            try: validate_context(ctx)
            except Exception: raise ToolFailure('INVALID_CONTEXT') from None
            identity=encoded(ctx)
            started=self.ledger.start_call(ctx['loopId'],identity,result['callId'],encoded(call))
            if started is None: raise ToolFailure('CONTEXT_CHANGED')
            record_id,prior=started
            if len(prior)>=3: raise ToolFailure('TOOL_LIMIT')
            if len(prior)>=2 and all(r[1] and json.loads(r[1])['status']=='rejected' for r in prior[-2:]):
                raise ToolFailure('INVALID_CALL_LIMIT')
            if self.clock()>=datetime.fromisoformat(ctx['deadlineAt']): raise ToolFailure('DEADLINE_EXCEEDED')
            try: validate_call(call)
            except Exception: raise ToolFailure('INVALID_ARGUMENTS') from None
            name,args=call['toolName'],call['arguments']
            if name not in ctx['allowedTools']: raise ToolFailure('NOT_ALLOWED')
            # A reused native ID must describe the same request.
            for previous,_ in prior:
                old=json.loads(previous)
                if old.get('callId')==call['callId'] and old!=call: raise ToolFailure('CALL_ID_CONFLICT')
            if name=='read_submission_result':
                data=self.backend.load_submission(ctx,args['submissionId'])
            else:
                if args['codeVersion'] not in ctx['allowedCodeVersions']: raise ToolFailure('NOT_FOUND_OR_FORBIDDEN')
                code=self.backend.load_code(ctx,args['codeVersion'])
                if code['codeVersion']!=args['codeVersion']: raise ToolFailure('SOURCE_MISMATCH')
                if hashlib.sha256(code['sourceCode'].encode('utf-8')).hexdigest()!=code['contentHash']:
                    raise ToolFailure('SOURCE_MISMATCH')
                if name=='read_code_excerpt': data=self._read(code,args)
                elif name=='search_code': data=self._search(code,args)
                else:
                    run_identity=encoded({'toolName':name,'codeVersion':code['codeVersion'],
                        'contentHash':code['contentHash'],'problemRef':ctx['problemRef'],
                        **({'experiment':args} if name=='run_local_code' else {})})
                    if not self.ledger.reserve_run(ctx['loopId'],run_identity): raise ToolFailure('RUN_LIMIT')
                    data=self.backend.run_local(ctx,code,args) if name=='run_local_code' else self.backend.run_public(ctx,code)
            if name=='read_submission_result':
                if data.get('submissionId')!=args['submissionId'] or data.get('problemRef')!=ctx['problemRef']:
                    raise ToolFailure('SOURCE_MISMATCH')
                if (data.get('status') not in ('awaitingAssets','queued','preparing','running','finished','failed','expired')
                        or type(data.get('codeVersion')) is not int or data['codeVersion']<1):
                    raise ToolFailure('INVALID_RESULT')
                if 'verdict' in data and (data['status']!='finished' or data['verdict'] not in ('AC','WA','TLE','RE','CE')):
                    raise ToolFailure('INVALID_RESULT')
            if name=='run_public_samples':
                if data.get('codeVersion')!=args['codeVersion']: raise ToolFailure('SOURCE_MISMATCH')
                if data.get('goalContext')!=ctx['goalContext'] or data.get('problemRef')!=ctx['problemRef']:
                    raise ToolFailure('SOURCE_MISMATCH')
                if data.get('status') not in ('succeeded','failed','cancelled','interrupted','running','waiting'):
                    raise ToolFailure('INVALID_RESULT')
                if data['status']=='succeeded':
                    cases=data.get('caseResults')
                    if data.get('sampleStatus') not in ('passed','failed') or not isinstance(cases,list) or not 1<=len(cases)<=8:
                        raise ToolFailure('INVALID_RESULT')
                    if any(not isinstance(c,dict) or not c.get('outcome') for c in cases):
                        raise ToolFailure('INVALID_RESULT')
                    if data['sampleStatus']=='passed' and any(c['outcome']!='passed' for c in cases):
                        raise ToolFailure('INVALID_RESULT')
            if name=='run_local_code':
                if data.get('codeVersion')!=args['codeVersion']: raise ToolFailure('SOURCE_MISMATCH')
                if data.get('goalContext')!=ctx['goalContext'] or data.get('problemRef')!=ctx['problemRef']:
                    raise ToolFailure('SOURCE_MISMATCH')
                if data.get('status') not in ('succeeded','failed','cancelled','interrupted','running','waiting'):
                    raise ToolFailure('INVALID_RESULT')
                if any(key in data for key in ('verdict','expectedOutput','correct','sampleStatus')):
                    raise ToolFailure('INVALID_RESULT')
                if data['status']=='succeeded':
                    if (not isinstance(data.get('observations'),dict)
                            or set(data['observations'])!=set(args['observe']['identifiers'])
                            or ('returnValue' in data)!=args['observe']['returnValue']
                            or data.get('setup')!=args['initialVariables']):
                        raise ToolFailure('INVALID_RESULT')
            if self.clock()>=datetime.fromisoformat(ctx['deadlineAt']): raise ToolFailure('DEADLINE_EXCEEDED')
            result.update(status='succeeded',data=data)
            if name in ('run_public_samples','run_local_code') and data.get('status') in ('failed','cancelled','interrupted','running','waiting','infrastructureError'):
                # Failed samples belong to a succeeded check; failed execution never does.
                result['status']='running' if data['status'] in ('waiting','running') else 'failed'
            if 'codeVersion' in data: result['codeVersion']=data['codeVersion']
            if len(encoded(result).encode('utf-8'))>16384:
                result.pop('data',None);raise ToolFailure('RESULT_TOO_LONG')
        except ToolFailure as error:
            result['status']='rejected' if 'data' not in result else 'failed'
            result.pop('data',None);result['errorCode']=str(error)
        except (KeyError,ValueError,TypeError,LookupError):
            result.update(status='failed',errorCode='NOT_FOUND_OR_FORBIDDEN');result.pop('data',None)
        except Exception:
            result.update(status='failed',errorCode='TOOL_UNAVAILABLE');result.pop('data',None)
        if record_id is not None:
            self.ledger.finish_call(record_id,encoded(result))
        return result

    @staticmethod
    def _lines(code):
        return code['sourceCode'].splitlines()

    def _read(self,code,args):
        lines=self._lines(code);start,end=args['startLine'],args['endLine']
        if start>end or end-start+1>120 or end>len(lines): raise ToolFailure('INVALID_RANGE')
        out=[]
        for n in range(start,end+1):
            item={'line':n,'text':lines[n-1]}
            if len(encoded([item]).encode('utf-8'))>8192: raise ToolFailure('LINE_TOO_LONG')
            if len(encoded(out+[item]).encode('utf-8'))>8192: break
            out.append(item)
        return {'codeVersion':code['codeVersion'],'contentHash':code['contentHash'],'lines':out,
            'startLine':start,'endLine':out[-1]['line'],'requestedEndLine':end,'totalLines':len(lines),
            'truncated':out[-1]['line']<end}

    def _search(self,code,args):
        query=args['query']
        if not query.strip() or '\n' in query or '\r' in query: raise ToolFailure('INVALID_ARGUMENTS')
        lines=self._lines(code);hits=[i+1 for i,line in enumerate(lines) if query in line];selected=hits[:10]
        positions=sorted({n for h in selected for n in range(max(1,h-2),min(len(lines),h+2)+1)})
        out=[]
        for n in positions:
            item={'line':n,'text':lines[n-1]}
            if len(encoded([item]).encode('utf-8'))>8192: raise ToolFailure('LINE_TOO_LONG')
            if len(encoded(out+[item]).encode('utf-8'))>8192: break
            out.append(item)
        visible={x['line'] for x in out}
        return {'codeVersion':code['codeVersion'],'contentHash':code['contentHash'],
            'matches':[n for n in selected if n in visible],'lines':out,'totalLines':len(lines),
            'truncated':len(hits)>10 or len(out)<len(positions)}
