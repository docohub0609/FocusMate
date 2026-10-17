"""Resolve model-selected fields from pinned facts; never trust supplied display text."""
from copy import deepcopy
import json
from focusmate.core.context_builder import hash_content


def _leaves(value,path=''):
    if isinstance(value,dict) and value:
        for key in sorted(value):
            if key=='evidenceText':continue
            yield from _leaves(value[key],path+'/'+key.replace('~','~0').replace('/','~1'))
    elif isinstance(value,list) and value:
        for index,item in enumerate(value):yield from _leaves(item,path+'/'+str(index))
    else:yield path,value


def fact_catalog(bundle):
    req=bundle['request']
    binding={k:req.get(k) for k in ('goalContext','problemRef','contractVersion','currentCodeRef','contractRef')}
    binding['checks']=bundle['checks']
    facts=[]
    for record in bundle['records']:
        if record['kind'] not in ('problemFact','runEvidence'):continue
        identity=[binding,record['ref'],record.get('version'),record.get('contentHash'),hash_content(record['content'])]
        for path,value in _leaves(record['content']):
            facts.append({'factId':'fact-'+hash_content([identity,path]).split(':')[-1],
                'ref':record['ref'],'path':path,'value':deepcopy(value)})
    return facts


def resolve_fact_quote(quote,catalog):
    fact=catalog.get(quote['factId'])
    if fact is None:raise ValueError('INVALID_FACT_REFERENCE')
    result={'ref':fact['ref'],'text':json.dumps({fact['path']:fact['value']},ensure_ascii=False,
        sort_keys=True,separators=(',',':')),'factId':fact['factId']}
    if any(quote[k]!=result[k] for k in ('ref','text') if k in quote):raise ValueError('FACT_QUOTE_MISMATCH')
    return result
