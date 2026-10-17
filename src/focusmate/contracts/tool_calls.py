"""Versioned, provider-independent tool contracts. Call context is caller-owned."""
from datetime import datetime
from jsonschema import Draft202012Validator

VERSION = '1.0.0'
def obj(properties, required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}
POS={'type':'integer','minimum':1}
TEXT={'type':'string','minLength':1,'maxLength':256}
IDENT={'type':'string','pattern':'^[A-Za-z_][A-Za-z0-9_]*$','maxLength':64}
JSON_VALUE={'type':['null','boolean','number','string','array','object']}
SNIPPET={'oneOf':[obj({'kind':{'const':'lines'},'startLine':POS,'endLine':POS}),
                    obj({'kind':{'const':'function'},'name':IDENT})]}
LOCAL_CALL=obj({'function':IDENT,'args':{'type':'array','maxItems':16,'items':JSON_VALUE}})
OBSERVE=obj({'identifiers':{'type':'array','maxItems':16,'uniqueItems':True,'items':IDENT},
             'returnValue':{'type':'boolean'}})
TOOL_PARAMETERS={
 'search_code':obj({'codeVersion':POS,'query':TEXT}),
 'read_code_excerpt':obj({'codeVersion':POS,'startLine':POS,'endLine':POS}),
 'read_submission_result':obj({'submissionId':TEXT}),
 'run_public_samples':obj({'codeVersion':POS}),
 'run_local_code':obj({'codeVersion':POS,'purpose':TEXT,'observationIntent':TEXT,
                       'snippet':SNIPPET,'initialVariables':{'type':'object','maxProperties':32,
                           'propertyNames':IDENT,'additionalProperties':JSON_VALUE},
                       'call':LOCAL_CALL,'observe':OBSERVE},
                      ['codeVersion','purpose','observationIntent','snippet','initialVariables','observe'])}
DESCRIPTIONS={
 'search_code':'在授权的已保存代码中搜索普通文字，返回前10处行号及附近原文。只在不知道位置且需要代码证据时使用。',
 'read_code_excerpt':'读取已保存代码指定的完整行，最多120行和8KiB。已有相关原文时不要重复读取；局部不能证明整份代码。',
 'read_submission_result':'查询已有提交的安全状态和结果，保留其代码版本。旧版本记录不是当前代码的验证；不重新运行。',
 'run_public_samples':'在获得运行许可时检查当前允许版本的公开样例，返回真实公开输入输出。仅需解释或已有足够证据时不要运行。通过不代表AC。',
 'run_local_code':'在获得运行许可时，从允许版本提取完整语句片段或顶层函数，用JSON初始值和固定模板运行。不携带函数外的import、全局对象或其他函数，不接受import语句、任意初始化脚本；依赖不能用JSON提供时先说明缺项，不编造已运行。必须说明运行目的和观察意图。结果只描述本次沙箱实验，不证明代码正确、AC或目标完成。'}
TOOL_DEFINITIONS=[{'type':'function','function':{'name':n,'description':DESCRIPTIONS[n],'parameters':s}} for n,s in TOOL_PARAMETERS.items()]
GOAL=obj({'goalId':TEXT,'scope':{'enum':['goal','subgoal']},'subgoalId':TEXT},['goalId','scope'])
CONTEXT_SCHEMA=obj({'loopId':TEXT,'goalContext':GOAL,'problemRef':obj({'problemId':TEXT,'problemVersion':POS}),
 'allowedCodeVersions':{'type':'array','minItems':0,'uniqueItems':True,'items':POS},
 'allowedTools':{'type':'array','uniqueItems':True,'items':{'enum':list(TOOL_PARAMETERS)}},
 'deadlineAt':TEXT})
CALL_SCHEMA=obj({'callId':TEXT,'toolName':TEXT,'arguments':{'type':'object'}})

def validate_context(value):
    Draft202012Validator(CONTEXT_SCHEMA).validate(value)
    goal=value['goalContext']
    if (goal['scope']=='subgoal') != ('subgoalId' in goal): raise ValueError('goal scope')
    if datetime.fromisoformat(value['deadlineAt']).tzinfo is None: raise ValueError('timezone')

def validate_call(value):
    Draft202012Validator(CALL_SCHEMA).validate(value)
    if value['toolName'] not in TOOL_PARAMETERS: raise ValueError('unknown tool')
    Draft202012Validator(TOOL_PARAMETERS[value['toolName']]).validate(value['arguments'])
