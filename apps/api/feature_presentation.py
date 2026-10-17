"""Lossless labels for the condition editor, never inferred problem facts."""
from copy import deepcopy
import re

CATEGORIES = {'dataScale': '数据范围', 'inputStructure': '输入要求', 'outputStructure': '输出要求',
              'resourceConstraint': '运行限制', 'domainTag': '题目涉及的数据'}
LABELS = {'variable': '变量', 'minimum': '最小值', 'maximum': '最大值', 'min': '最小值', 'max': '最大值',
          'kind': '数据形式', 'type': '类型', 'lengthVariable': '长度变量', 'valueCount': '数量',
          'valueType': '数值类型', 'meanings': '含义', 'elementType': '元素类型', 'unit': '单位',
          'nMax': '最大规模', 'nMin': '最小规模', 'count': '数量', 'base': '起始编号'}


def value_paths(value, path='/value', depth=0):
    if depth > 16:
        raise ValueError('Value is too deeply nested')
    yield path
    if isinstance(value, dict):
        for key, item in value.items():
            yield from value_paths(item, path + '/' + key.replace('~', '~0').replace('/', '~1'), depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from value_paths(item, path + '/' + str(index), depth + 1)


def required_paths(feature):
    paths = set(value_paths(feature['value'])) if 'value' in feature else set()
    for index, candidate in enumerate(feature.get('candidates', [])):
        paths.update(value_paths(candidate['value'], f'/candidates/{index}/value'))
    return paths


def complete_presentation(feature):
    data = feature.get('presentation', {})
    try:
        paths = required_paths(feature)
        labels = data.get('labels', {})
        return bool(re.search('[\u4e00-\u9fff]', data.get('title', ''))) and set(labels) == paths and all(
            isinstance(label, str) and bool(re.search('[\u4e00-\u9fff]', label)) for label in labels.values())
    except (TypeError, ValueError):
        return False


def with_legacy_labels(feature):
    """Only a fixed dictionary, never guess the meaning of arbitrary old keys."""
    result = deepcopy(feature)
    if 'presentation' in result:
        return result
    labels = {}
    for path in required_paths(result):
        key = path.rsplit('/', 1)[-1].replace('~1', '/').replace('~0', '~')
        if key == 'value':
            label = '条件内容'
        elif key.isdigit():
            label = '第' + str(int(key) + 1) + '项'
        else:
            label = LABELS.get(key)
        if label is None:
            return result
        labels[path] = label
    result['presentation'] = {'title': CATEGORIES[result['category']], 'labels': labels}
    return result


def public_report(report):
    result = deepcopy(report)
    result['features'] = [with_legacy_labels(item) for item in result['features']]
    return {'report': result, 'displayComplete': all(complete_presentation(item) for item in result['features'])}


def fill_fixed_labels(feature):
    """Fill only missing, unambiguous UI names; preserve model labels and facts."""
    data = feature.get('presentation')
    if not isinstance(data, dict) or not isinstance(data.get('labels'), dict):
        return
    for path in required_paths(feature):
        if path in data['labels']:
            continue
        key = path.rsplit('/', 1)[-1].replace('~1', '/').replace('~0', '~')
        label = '条件内容' if key == 'value' else ('第' + str(int(key)+1) + '项' if key.isdigit() else LABELS.get(key))
        if label is not None:
            data['labels'][path] = label
