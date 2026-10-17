"""Check condition relevance and code evidence in one verifier call."""
from copy import deepcopy
from jsonschema import Draft202012Validator
from focusmate.contracts.process_verification import FACT_MODEL_RESULT
from focusmate.core.process_verification import finish_verification


CODE_RANGE_SCHEMA={'type':'object','properties':{
    'codeRef':{'type':'string','minLength':1},
    'startLine':{'type':'integer','minimum':1},
    'endLine':{'type':'integer','minimum':1}},
    'required':['codeRef','startLine','endLine'],'additionalProperties':False}
MESSAGE_SCHEMA={'type':'object','properties':{'messageRef':{'type':'string','minLength':1}},
    'required':['messageRef'],'additionalProperties':False}


def message_catalog(bundle):
    return {r['ref']:r['content']['text'] for r in bundle['records']
        if r['kind']=='dialogue' and r['content'].get('role')=='user'
        and isinstance(r['content'].get('text'),str) and r['content']['text'].strip()}


def code_line_catalog(bundle):
    """Only expose the current, actually loaded source; never expand hidden code."""
    current=bundle['request']['currentCodeRef']
    return [{'codeRef':r['ref'],'lines':[{'line':i,'text':line}
            for i,line in enumerate(r['content']['text'].splitlines(keepends=True),1)]}
        for r in bundle['records'] if r['kind']=='code' and r['ref']==current
        and isinstance(r['content'].get('text'),str)
        and len(r['content']['text'].encode())<=8192]


def condition_result_schema():
    schema=deepcopy(FACT_MODEL_RESULT)
    item=schema['properties']['checks']['items']
    item['properties']['conditionStatus']={'enum':['valid','invalid','uncertain']}
    item['required'].append('conditionStatus')
    item['required'].remove('status')
    item['properties']['status']['description']='Required only when conditionStatus is valid. Otherwise omit; the server records inconclusive.'
    item['properties']['quotes']['items']['anyOf'].append(deepcopy(CODE_RANGE_SCHEMA))
    item['properties']['quotes']['items']['anyOf'].append(deepcopy(MESSAGE_SCHEMA))
    return schema


def finish_condition_verification(value,bundle):
    Draft202012Validator(condition_result_schema()).validate(value)
    plain=deepcopy(value);conditions={}
    sources={x['codeRef']:x['lines'] for x in code_line_catalog(bundle)}
    messages=message_catalog(bundle)
    for item in plain['checks']:
        condition=item.pop('conditionStatus')
        # A rejected/unknown condition cannot decide whether code is correct.
        # Derive that consequence instead of asking the model to repeat it in
        # a second field. The original response remains in the step audit.
        if condition!='valid':
            item['status']='inconclusive'
        conditions[item['checkId']]=condition
        for index,quote in enumerate(item['quotes']):
            if 'messageRef' in quote:
                key=quote['messageRef']
                if key not in messages:raise ValueError('INVALID_USER_MESSAGE_REFERENCE')
                item['quotes'][index]={'ref':key,'text':messages[key]}
                continue
            if 'codeRef' not in quote:continue
            lines=sources.get(quote['codeRef']);start=quote['startLine'];end=quote['endLine']
            if lines is None or not 1<=start<=end<=len(lines):
                from focusmate.core.process_verification import EvidenceCheckError
                raise EvidenceCheckError('INVALID_CODE_RANGE',item['checkId'])
            item['quotes'][index]={'ref':quote['codeRef'],
                'text':''.join(x['text'] for x in lines[start-1:end])}
    report=finish_verification(plain,bundle)
    for item in report['checks']:item['conditionStatus']=conditions[item['checkId']]
    return report


CONDITION_PROMPT='''逐项先判断requirement本身是否成立、是否对应当前用户问题和题目要求，再判断代码是否满足。目标、历史提示和局部报告都可能包含教练的错误猜测，保存或以前被判valid不使它变成事实。
核对用户实际变量含义、各分支前后的不变性质及完整流程，不按熟悉的模板猜测变量用途。允许不同但正确的算法；“没有照旧提示修改”不是失败依据。用户质疑条件或给出同版本通过记录时，应先检查原条件是否额外强加了某种写法，而不是替旧结论找理由。
conditionStatus=valid表示条件合理；invalid表示条件错置、额外强加算法或混入教练操作；uncertain表示无法确定。
valid时必须填写status判断代码；invalid/uncertain时不填status，在reason说明条件的问题和依据。后台统一记录inconclusive，不据此判代码通过或失败。
不得偷偷改写成功条件；需要调整时由后续正常规划保存新版本或撤回，修复Agent不获得修改既有条件的权限。valid时仍须有足够证据；阅读代码不能冒充实际执行。引用反例须核实完整路径、预期和实际结果；只有静态推演就注明未实测，不能把猜测写成真实失败。
同版本公开运行只覆盖所执行样例，自定义运行没有标准答案时不能证明正确；可信判题与程序最终核验有各自范围，不自动证明所有局部性质，也不能被未经核实的旧模型断言否定。
引用当前代码时从codeLineCatalog选行号，填写{"codeRef":"目录中的代码编号","startLine":起始行,"endLine":结束行}，行号从1开始且两端包含。不重新抄写代码或缩进。程序从冻结代码中提取原文。目录为空时不能编造行号；代码摘录工具所得材料仍须引用实际原文。'''
CONDITION_PROMPT+='\n引用用户解释时从userMessageCatalog选择messageRef，填写{"messageRef":"消息编号"}，后台还原该条原话。消息存在不代表解释正确或对应当前代码，仍须判断内容和版本。'
