"""Project checked tool receipts without changing their source context."""
from copy import deepcopy
import json
from focusmate.core.context_builder import hash_content

def collect_evidence(bundle,state,step):
    extra=[];history=[]
    rounds={step['roundId'],*state['rounds'][step['roundId']].get('verificationEvidenceRoundIds',[])}
    for st in state['steps'].values():
        if st['roundId'] not in rounds or st['kind']!='tool' or st['status']!='done':continue
        result=st.get('result',{}).get('result')
        if not result:continue
        call=st['payload']
        history.extend([{'role':'assistant','content':'','tool_calls':[{'id':call['callId'],'type':'function',
            'function':{'name':call['toolName'],'arguments':json.dumps(call['arguments'],ensure_ascii=False)}}]},
            {'role':'tool','tool_call_id':call['callId'],'content':json.dumps(result,ensure_ascii=False)}])
        content=deepcopy(result.get('data',{}))
        if result.get('status') in ('failed','rejected'):raise ValueError('VERIFICATION_TOOL_UNAVAILABLE')
        run=call['toolName'] in ('run_public_samples','run_local_code','read_submission_result')
        content['codeSnapshotRef']=bundle['request']['currentCodeRef'] if result.get('codeVersion')==state['rounds'][step['roundId']]['codeVersion'] else None
        if run:
            content['completed']=result.get('status')=='succeeded' and (call['toolName']!='read_submission_result' or content.get('status')=='finished')
        record={'ref':'tool:'+call['callId'],'kind':'runEvidence' if run else 'codeExcerpt',
            'version':1,'goalContext':deepcopy(result['goalContext']),'content':content,'contentHash':hash_content(content),'origin':'checked-tool'}
        extra.append(record)
    return extra,history
