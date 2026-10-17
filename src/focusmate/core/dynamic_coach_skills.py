"""Pinned, bounded Skill reading; instructions are not verification evidence."""
from jsonschema import Draft202012Validator
from focusmate.core.skill_sections import load_index,load_sections

PARAMETERS={'type':'object','properties':{'skillId':{'type':'string'},'version':{'type':'integer'},
    'hash':{'type':'string'},'sectionIds':{'type':'array','minItems':1,'maxItems':2,'uniqueItems':True,'items':{'type':'string'}}},
    'required':['skillId','version','hash','sectionIds'],'additionalProperties':False}
DEFINITION={'type':'function','function':{'name':'read_skill','description':'选择目录中一份Skill的最多两个小节。只读检查方法，不是完成证据。','parameters':PARAMETERS}}


def read_skill(call,catalog):
    args=call['arguments'];Draft202012Validator(PARAMETERS).validate(args)
    pin=next((x for x in catalog if x['skillId']==args['skillId'] and x['version']==args['version'] and x['hash']==args['hash']),None)
    if pin is None:raise ValueError('SKILL_PIN_MISMATCH')
    index=load_index(pin['path'],pin['version'],pin['hash'])
    return {'kind':'skill','callId':call['callId'],'content':{**args,**load_sections(index,args['sectionIds']),
        'semanticStatus':'instructionsOnly'}}
