"""Offline JSON Schema references, including unexercised property branches."""
from copy import deepcopy
from urllib.parse import quote, urljoin, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from focusmate.contracts.schema_validation import SCHEMA_ROOT
from focusmate.contracts.skill_files import PackageError, fail, local_reference, strict_json

DIALECT = 'https://json-schema.org/draft/2020-12/schema'
BASE = 'https://skill-package.local/'
MAPS = {'properties', 'patternProperties', '$defs', 'dependentSchemas'}
ARRAYS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}
SINGLE = {'additionalProperties', 'unevaluatedProperties', 'propertyNames', 'items',
          'unevaluatedItems', 'contains', 'not', 'if', 'then', 'else', 'contentSchema'}


def nodes(schema):
    yield schema
    if not isinstance(schema, dict):
        return
    for key, value in schema.items():
        children = value.values() if key in MAPS else value if key in ARRAYS else [value] if key in SINGLE else []
        for child in children:
            yield from nodes(child)


def _package_registry(files):
    parsed = {path: strict_json(text) for path, text in files.items()
              if path.endswith('.json') and path != 'descriptor.json'}
    registered = {}
    for path in SCHEMA_ROOT.rglob('*.schema.json'):
        schema = strict_json(path.read_text(encoding='utf-8'))
        registered[schema['$id']] = Resource.from_contents(schema)
    resources = {BASE + quote(path): Resource.from_contents(value, default_specification=DRAFT202012)
                 for path, value in parsed.items() if isinstance(value, (dict, bool))}
    # Explicit Registry has no retrieval callback: missing refs fail offline.
    registry = Registry().with_resources([*registered.items(), *resources.items()])
    return parsed, registered, registry


def package_validator(files, path):
    """Validate with the same offline package base/catalog as check_schemas.

    The caller must first validate the package; this helper is not a replacement
    for descriptor, path, schema-branch or package identity checks.
    """
    parsed, _, registry = _package_registry(files)
    return Draft202012Validator(parsed[path], registry=registry,
                                format_checker=FormatChecker(),
                                _resolver=registry.resolver(BASE + quote(path)))


def _model_schema(value, schema_ids):
    """Strip annotations only from visited schemas, including reference targets.

    Container keys and const/enum values are data, not schema keywords.
    """
    if isinstance(value, list):
        return [_model_schema(item, schema_ids) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    clean = {}
    is_schema = id(value) in schema_ids
    for key, item in value.items():
        if is_schema and key in ('examples', 'default'):
            continue
        if is_schema and key in ('const', 'enum'):
            clean[key] = deepcopy(item)
        else:
            clean[key] = _model_schema(item, schema_ids)
    return clean


def package_schema_materials(files, path):
    """Return model schema plus only its transitively referenced documents.

    Resources use canonical URI keys and explicit bases so relative refs remain
    interpretable. Cycles/internal fragments do not duplicate the root. Missing
    references raise without fetching a URL. Neither examples nor defaults are
    coaching evidence, so they are removed from the entire returned material.
    """
    parsed, _, registry = _package_registry(files)
    root_uri = BASE + quote(path)
    documents = {root_uri: parsed[path]}
    pending = [(root_uri, parsed[path])]
    visited, schema_ids = set(), set()
    while pending:
        uri, schema = pending.pop()
        identity = (uri, id(schema))
        if identity in visited:
            continue
        visited.add(identity)
        resolver = registry.resolver(uri)
        for node in nodes(schema):
            schema_ids.add(id(node))
            if not isinstance(node, dict):
                continue
            for key in ('$ref', '$dynamicRef'):
                if key not in node:
                    continue
                ref = node[key]
                # Resolving also checks that anchors/fragments actually exist.
                resolved = resolver.lookup(ref)
                document = urljoin(uri, ref).split('#', 1)[0]
                if document not in documents:
                    documents[document] = registry.get_or_retrieve(document).value.contents
                    pending.append((document, documents[document]))
                # Pointers may target valid schemas inside extension fields,
                # which nodes(document) deliberately does not treat as schemas.
                pending.append((document, resolved.contents))
    root = _model_schema(documents.pop(root_uri), schema_ids)
    resources = {uri: _model_schema(schema, schema_ids) for uri, schema in sorted(documents.items())}
    if resources:
        if isinstance(root, dict):
            root.setdefault('$id', root_uri)
        for uri, schema in resources.items():
            if isinstance(schema, dict):
                schema.setdefault('$id', uri)
    return {'schema': root, 'resources': resources}


def check_schemas(files, descriptor):
    try:
        parsed, registered, registry = _package_registry(files)
        roots = {path for path in parsed if path.endswith('.schema.json')}
        for field in ('inputSchemaRef', 'outputSchemaRef'):
            ref = descriptor[field]
            if urlsplit(ref).fragment:
                raise ValueError('root must be file')
            roots.add(local_reference('descriptor.json', ref, files))
        pending = [(path, parsed[path]) for path in sorted(roots)]
        visited = set()
        while pending:
            path, schema = pending.pop()
            identity = (path, id(schema))
            if identity in visited:
                continue
            visited.add(identity)
            Draft202012Validator.check_schema(schema)
            if schema is parsed[path] and (not isinstance(schema, dict) or schema.get('$schema') != DIALECT):
                raise ValueError('explicit supported dialect required')
            base = BASE + quote(path)
            resolver = registry.resolver(base)
            for node in nodes(schema):
                if not isinstance(node, dict):
                    continue
                if '$schema' in node and node['$schema'] != DIALECT:
                    raise ValueError('mixed dialects')
                if '$id' in node and (node is not parsed[path] or node['$id'] != base):
                    raise ValueError('ambiguous schema base')
                for key in ('$ref', '$dynamicRef'):
                    if key not in node:
                        continue
                    ref = node[key]
                    absolute = urljoin(base, ref)
                    document = absolute.split('#', 1)[0]
                    if document not in registered:
                        target = local_reference(path, ref, files)
                        if document != BASE + quote(target):
                            raise ValueError('noncanonical reference')
                        pending.append((target, parsed[target]))
                    resolved = resolver.lookup(ref)
                    # A valid JSON Pointer may point to a string or to a schema in
                    # an extension field. Check target type AND transitive refs.
                    Draft202012Validator.check_schema(resolved.contents)
                    if document not in registered:
                        pending.append((target, resolved.contents))
                validator = Draft202012Validator(node, registry=registry, format_checker=FormatChecker(), _resolver=resolver)
                for example in node.get('examples', []):
                    validator.validate(example)
    except PackageError as error:
        fail('INVALID_SCHEMA', error.issue['path'], '输入输出 Schema 的文件引用无效。')
    except Exception:
        fail('INVALID_SCHEMA', locals().get('path', 'schemas'), 'Schema、引用或示例不符合离线 Draft 2020-12 约定。')
