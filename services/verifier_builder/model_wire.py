"""Builder identity belongs to the claimed job, not generated model text."""
from focusmate.contracts.model_bindings import omit_bound_fields,restore_bound_fields


def bindings(context,role=None):
    result={key:context[key] for key in ('schemaVersion','problemRef','problemContentHash','featureSetId')}
    if role is not None:result['role']=role
    return result


def response_schema(schema,context,role=None):
    return omit_bound_fields(schema,bindings(context,role))


def response_document(value,context,role=None):
    return restore_bound_fields(value,bindings(context,role))
