"""Wire contracts for a single-subgoal process verification."""
from copy import deepcopy
TEXT = {'type': 'string', 'minLength': 1}
def obj(properties, required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}
def array(items, minimum=0):
    return {'type':'array','items':items,'minItems':minimum}
CONTEXT={'$ref':'https://focusmate.local/schemas/common/goal-context.schema.json'}
PROBLEM={'$ref':'https://focusmate.local/schemas/common/definitions.schema.json#/$defs/problemRef'}
SOURCE=obj({'ref':TEXT,'kind':TEXT,'version':{'type':['integer','string']},'contentHash':TEXT,
            'sourceType':TEXT,'sourceId':TEXT},['ref','kind','version','contentHash'])
BUDGET=obj({'contextWindowTokens':{'type':'integer','minimum':1},'reservedOutputTokens':{'type':'integer','minimum':1},
            'safetyMarginTokens':{'type':'integer','minimum':0}})
REQUEST=obj({'schemaVersion':{'const':'1.0.0'},'goalContext':CONTEXT,'problemRef':PROBLEM,
    'contractVersion':{'type':'integer','minimum':1},'contractRef':TEXT,'actionId':TEXT,
    'currentCodeRef':{'type':['string','null'],'minLength':1},'materialRefs':array(TEXT,1),
    'sources':array(SOURCE,1),'budget':BUDGET})
EVIDENCE=['confirmedProblemFeature','evaluatedUserExplanation','complexityCheck','codeStructureCheck',
          'compileResult','runResult','publicSampleRunResult','targetedRunResult']
QUOTE=obj({'ref':TEXT,'text':TEXT,'factId':TEXT},['ref','text'])
CHECK=obj({'checkId':TEXT,'status':{'enum':['passed','failed','notReady','inconclusive']},'reason':TEXT,
    'quotes':array(QUOTE,1),'evidenceType':{'enum':EVIDENCE},'limitations':array(TEXT)})
MODEL_RESULT=obj({'checks':array(CHECK,1)})
FACT_MODEL_RESULT=deepcopy(MODEL_RESULT)
FACT_MODEL_RESULT['properties']['checks']['items']['properties']['quotes']['items']={
    'anyOf':[deepcopy(QUOTE),obj({'factId':TEXT})]}
REPORT=obj({'schemaVersion':{'const':'1.0.0'},'reportId':TEXT,'actionId':TEXT,
    'verifierResult':{'$ref':'https://focusmate.local/schemas/execution/verifier-result.schema.json'},
    'checks':array(CHECK,1),'requirements':array({'type':'object'},1),'inputSnapshot':{'type':'object'},
    'method':{'const':'programFactsAndSingleModelRole'},'progressComparison':{'const':'notEstablished'},
    'promptVersion':TEXT})
