"""Reviewers select a candidate field; the server supplies its exact text."""
from copy import deepcopy


def candidate_fields(candidate):
    fields={key:candidate[key] for key in ('content','reason')}
    fields.update({'followUp.'+key:candidate['followUp'][key] for key in ('reason','expectedUserAction')})
    for key in ('diagnosisCorrections','requestQuotes'):
        for i,item in enumerate(candidate[key]):
            field='reason' if key=='diagnosisCorrections' else 'text'
            fields[f'{key}.{i}.{field}']=item[field]
    result={}
    for key,value in fields.items():
        if not value.strip():continue
        if len(value)<=4000:result[key]=value
        else:
            for offset in range(0,len(value),2000):
                part=value[offset:offset+2000]
                if part.strip():result[f'{key}@{offset}']=part
    return result


def review_location_schema(schema,candidate):
    result=deepcopy(schema);fields=candidate_fields(candidate)
    for item in result['properties']['actionReview']['properties']['checks']['properties'].values():
        item['properties'].pop('candidateQuote');item['required'].remove('candidateQuote')
        item['properties']['candidateField']={'enum':[None,*fields]}
        item['required'].append('candidateField')
    return result


def restore_review_locations(review,candidate):
    result=deepcopy(review);fields=candidate_fields(candidate)
    for item in result['actionReview']['checks'].values():
        if 'candidateField' not in item:continue  # Legacy reply still faces strict quote validation.
        key=item.pop('candidateField')
        if key is not None and key not in fields:raise ValueError('INVALID_REVIEW_FIELD')
        text='' if key is None else fields[key]
        if 'candidateQuote' in item and item['candidateQuote']!=text:raise ValueError('REVIEW_FIELD_CONFLICT')
        item['candidateQuote']=text
    return result
