"""Validate a static Skill package. Never register, execute or review its meaning."""
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
import yaml

from focusmate.contracts.schema_validation import validate
from focusmate.contracts.skill_files import PackageError, fail, fingerprint, local_reference, read_files, strict_json
from focusmate.contracts.skill_schemas import check_schemas

SECTIONS = ('适用条件', '目标', '所需输入', '处理方法', '禁止行为', '结果格式')


class UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError('duplicate YAML key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def document(text, name, *, with_metadata=False):
    try:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != '---':
            raise ValueError('missing frontmatter')
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
        header = ''.join(lines[1:end])
        if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in yaml.scan(header)):
            raise ValueError('YAML aliases unsupported')
        meta = yaml.load(header, Loader=UniqueSafeLoader)
        if not isinstance(meta, dict) or not {'name', 'description'} <= meta.keys():
            raise ValueError('required metadata')
        if set(meta) - {'name', 'description', 'license', 'compatibility', 'metadata'}:
            raise ValueError('unsupported metadata')
        if meta['name'] != name or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
            raise ValueError('name')
        for key, maximum in [('description', 1024), ('license', 1024), ('compatibility', 500)]:
            if key in meta and (not isinstance(meta[key], str) or not 1 <= len(meta[key].strip()) <= maximum):
                raise ValueError('metadata text')
        if 'metadata' in meta and (not isinstance(meta['metadata'], dict) or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in meta['metadata'].items())):
            raise ValueError('metadata mapping')
    except Exception:
        fail('INVALID_FRONTMATTER', 'SKILL.md', '名称、简介或 YAML 元数据不符合规范。')
    body = ''.join(lines[end + 1:])
    return (body, meta) if with_metadata else body


def markdown_checks(files, body):
    parser = MarkdownIt('commonmark')
    # Inspect even unsafe-scheme links instead of the renderer silently treating
    # them as plain text. Nothing is rendered/executed; our path policy rejects them.
    parser.validateLink = lambda url: True
    tokens = parser.parse(body)
    sections = {name: [] for name in SECTIONS}
    for i, token in enumerate(tokens):
        if token.type != 'heading_open' or token.tag != 'h2' or token.level != 0:
            continue
        title = tokens[i + 1].content.strip()
        if title not in sections:
            continue
        end = next((j for j in range(i + 3, len(tokens)) if tokens[j].type == 'heading_open'
                    and tokens[j].tag in ('h1', 'h2') and tokens[j].level == 0), len(tokens))
        sections[title].append(any(t.type in ('inline', 'fence', 'code_block') and t.content.strip() for t in tokens[i + 3:end]))
    if any(values != [True] for values in sections.values()):
        fail('INVALID_SECTIONS', 'SKILL.md', '六个必需二级章节必须各出现一次且有正文，示例中的标题不算章节。')
    for path, text in files.items():
        if not path.endswith('.md'):
            continue
        for token in parser.parse(body if path == 'SKILL.md' else text):
            if token.type == 'html_block' or any(c.type == 'html_inline' for c in token.children or []):
                fail('INVALID_REFERENCE', path, '本版只支持 Markdown 链接，不接受原生 HTML。')
            for child in token.children or []:
                if child.type not in ('link_open', 'image'):
                    continue
                target = child.attrGet('href') if child.type == 'link_open' else child.attrGet('src')
                if urlsplit(target).scheme in ('http', 'https', 'mailto'):
                    continue  # Bibliographic link only; never fetch remote material.
                local_reference(path, target, files)


def validate_skill_package(package_dir):
    result = {'packageValid': False, 'descriptor': None, 'metadata': None, 'computedHash': None, 'fileManifest': [],
              'declaredToolRequirements': [], 'toolAvailability': 'notChecked', 'contentReview': 'notPerformed', 'issues': []}
    try:
        files = read_files(package_dir)
        try:
            descriptor = strict_json(files['descriptor.json'])
            if not isinstance(descriptor, dict):
                raise ValueError('descriptor must be object')
        except Exception:
            fail('INVALID_DESCRIPTOR', 'descriptor.json', '描述文件不是有效且无重复键的 JSON 对象。')
        result['computedHash'], result['fileManifest'] = fingerprint(files, descriptor)
        try:
            validate('SkillDescriptor', descriptor)
            if not {'requiredTools', 'goalStateWriteAllowed'} <= descriptor.keys():
                raise ValueError('new packages require explicit policies')
            if descriptor['skillId'] != Path(package_dir).absolute().name:
                raise ValueError('name mismatch')
            if datetime.fromisoformat(descriptor['createdAt'].replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('timestamp requires timezone')
            for field in ('appliesTo', 'capabilities', 'requiredInputs'):
                if any(not value.strip() for value in descriptor[field]):
                    raise ValueError('blank metadata')
        except Exception:
            fail('INVALID_DESCRIPTOR', 'descriptor.json', '描述字段、名称、时间或 Coach/状态写入声明不合法。')
        result['descriptor'] = descriptor
        result['declaredToolRequirements'] = list(descriptor['requiredTools'])
        # Independent structural checks collect actionable categories without interpreting prose.
        def check_document():
            body, metadata = document(files['SKILL.md'], descriptor['skillId'], with_metadata=True)
            result['metadata'] = metadata
            markdown_checks(files, body)

        checks = [check_document,
                  lambda: check_schemas(files, descriptor)]
        for check in checks:
            try:
                check()
            except PackageError as error:
                result['issues'].append(error.issue)
        if result['computedHash'] != descriptor['hash']:
            result['issues'].append({'code': 'HASH_MISMATCH', 'path': 'descriptor.json.hash',
                                     'message': '内容指纹与包内容不一致，请核对修改后显式更新。'})
    except PackageError as error:
        result['issues'].append(error.issue)
    except (OSError, ValueError, TypeError, RecursionError):
        result['issues'].append({'code': 'INVALID_DIRECTORY', 'path': '.', 'message': '包目录不存在、不可读或无法安全解析。'})
    result['issues'].sort(key=lambda item: (item['path'], item['code']))
    result['packageValid'] = not result['issues']
    return result
