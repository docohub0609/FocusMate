"""Model presentation only: saved evidence and validators retain their originals."""
from copy import deepcopy


def fact_locations(catalog):
    # Values already exist at (ref, JSON pointer) in materials. The full catalog
    # remains the validator's source, so IDs keep their original content binding.
    return [{key:fact[key] for key in ('factId','ref','path')} for fact in catalog]


def model_records(records):
    result=deepcopy(records)
    for record in result:
        content=record['content']
        if record['kind'] in ('problemFact','runEvidence'):
            content.pop('evidenceText',None)
        if record['kind']=='verifiedFact' and 'inputSnapshot' in content:
            content.pop('inputSnapshot')
            record['presentation']='report conclusion and evidence quotes; archived input snapshot omitted'
    return result


def repair_packet(record):
    """Expose the rejected output, not its recursively embedded request audit.

    This is not a recursive key filter: arbitrary fields inside the model's
    candidate remain intact, including malformed ones needed for correction.
    """
    failure=deepcopy(record['failure'])
    original=deepcopy(record['original'])
    while isinstance(original,dict) and original.get('kind')=='failure' and 'rejectedResult' in original:
        original=original['rejectedResult']
    if isinstance(original,dict):
        for key in ('contextSnapshot','modelConfig','usage','responseNormalization','responseAudit','goalRequirementAudit'):
            original.pop(key,None)
    failure.pop('rejectedResult',None)
    if failure.get('responseText')==original:failure.pop('responseText')
    diagnostic=failure.get('responseDiagnostic')
    if isinstance(diagnostic,dict) and diagnostic.get('text')==original:
        diagnostic.pop('text')
    # Provider timing/request envelopes belong to the private failure archive.
    failure.pop('responseAudit',None)
    return {'failure':failure,'materials':deepcopy(record['materials']),'original':original}
