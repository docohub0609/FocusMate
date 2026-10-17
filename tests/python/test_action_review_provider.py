import json
import pytest
from apps.api.deepseek_provider import DeepSeekConfig
from focusmate.adapters.tool_chat import ToolChatError


@pytest.mark.parametrize('thinking',[False,True])
def test_review_transport_modes_single_request_without_reasoning_log(thinking):
    from focusmate.adapters.action_review import DeepSeekActionReviewProvider
    calls=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return json.dumps({'model':'actual','choices':[{'finish_reason':'stop','message':{
            'content':'{"checks":{}}','reasoning_content':'PRIVATE'}}]}).encode()
    def opener(req,timeout):calls.append((json.loads(req.data),timeout));return Response()
    p=DeepSeekActionReviewProvider(DeepSeekConfig('secret','https://api.deepseek.com','m'),
                                  thinking=thinking,opener=opener,clock=lambda:10)
    result=p.complete(messages=[{'role':'user','content':'review'}],tools=[],deadline=20)
    assert len(calls)==1 and calls[0][1]==10
    assert calls[0][0]['thinking']['type']==('enabled' if thinking else 'disabled')
    assert calls[0][0]['response_format']=={'type':'json_object'}
    assert 'tools' not in calls[0][0]
    assert ('temperature' in calls[0][0]) is not thinking
    assert 'PRIVATE' not in json.dumps(result) and 'secret' not in json.dumps(p.public_config)
    with pytest.raises(ToolChatError,match='REVIEW_TOOLS_FORBIDDEN'):
        p.complete(messages=[],tools=[{'name':'run'}],deadline=20)
    assert len(calls)==1
