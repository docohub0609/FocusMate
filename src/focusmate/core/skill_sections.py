"""Pinned package indexes and bounded selective prompt loading, without execution.

load_index(path, version, expected_hash=None) validates the whole package and
returns immutable metadata only. load_sections(snapshot, section_ids) revalidates
its on-disk identity/hash, returning {'common': body, 'sections': {id: body}}.
The complete package is read for integrity; only selected bodies reach callers.
"""
from dataclasses import dataclass
from pathlib import Path
import re

from focusmate.contracts.skill_files import fail, fingerprint, read_files, strict_json
from focusmate.core.skill_package import document, validate_skill_package

INDEX_PATH = 'references/index.json'


@dataclass(frozen=True)
class SkillSection:
    section_id: str
    description: str
    file_ref: str


@dataclass(frozen=True)
class SkillIndexSnapshot:
    package_path: Path
    skill_id: str
    version: int
    hash: str
    sections: tuple[SkillSection, ...]


def _validated_files(path, version, expected_hash):
    if type(version) is not int:
        raise TypeError('version 必须是明确的整数版本。')
    report = validate_skill_package(path)
    if not report['packageValid']:
        fail('INVALID_PACKAGE', path, 'Skill 包校验失败。')
    descriptor = report['descriptor']
    if descriptor['version'] != version or (expected_hash is not None and descriptor['hash'] != expected_hash):
        fail('IDENTITY_MISMATCH', path, 'Skill 版本或指纹与指定版本不一致。')
    files = read_files(path)
    # Verify the actual buffers returned, closing drift between validation/read.
    current_descriptor = strict_json(files['descriptor.json'])
    if current_descriptor != descriptor or fingerprint(files, current_descriptor)[0] != descriptor['hash']:
        fail('PACKAGE_DRIFT', path, 'Skill 包在读取期间变化。')
    return descriptor, files


def _index(files, descriptor):
    try:
        value = strict_json(files[INDEX_PATH])
        if set(value) != {'skillId', 'version', 'sections'} or value['skillId'] != descriptor['skillId'] or type(value['version']) is not int or value['version'] != descriptor['version']:
            raise ValueError('identity')
        if not isinstance(value['sections'], list) or not 1 <= len(value['sections']) <= 32:
            raise ValueError('sections')
        sections, ids, paths = [], set(), set()
        for entry in value['sections']:
            if set(entry) != {'sectionId', 'description', 'fileRef'}:
                raise ValueError('fields')
            sid, description, path = entry['sectionId'], entry['description'], entry['fileRef']
            if not isinstance(sid, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', sid) or len(sid) > 64 or sid in ids:
                raise ValueError('section ID')
            if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
                raise ValueError('description')
            # Canonical package-root relative Markdown only; no decoding/normalization.
            if not isinstance(path, str) or not re.fullmatch(r'references/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+\.md', path) or path not in files or path in paths or not files[path].strip():
                raise ValueError('fileRef')
            ids.add(sid)
            paths.add(path)
            sections.append(SkillSection(sid, description, path))
        return tuple(sections)
    except (KeyError, TypeError, ValueError):
        fail('INVALID_SECTION_INDEX', INDEX_PATH, '分节索引的身份、章节或包内路径不合法。')


def load_index(package_path, version, expected_hash=None):
    """Return SkillIndexSnapshot of ID/version/hash and section metadata; no bodies."""
    path = Path(package_path).absolute()
    descriptor, files = _validated_files(path, version, expected_hash)
    return SkillIndexSnapshot(path, descriptor['skillId'], version, descriptor['hash'], _index(files, descriptor))


def load_sections(snapshot, section_ids):
    """Return common body and 1–2 uniquely selected bodies after integrity recheck."""
    if not isinstance(snapshot, SkillIndexSnapshot):
        raise TypeError('需要 load_index 返回的 SkillIndexSnapshot。')
    if not isinstance(section_ids, (list, tuple)) or not 1 <= len(section_ids) <= 2 or any(not isinstance(sid, str) for sid in section_ids) or len(set(section_ids)) != len(section_ids):
        fail('INVALID_SECTION_SELECTION', INDEX_PATH, '必须选择一至两个不重复的章节 ID。')
    descriptor, files = _validated_files(snapshot.package_path, snapshot.version, snapshot.hash)
    sections = _index(files, descriptor)
    if descriptor['skillId'] != snapshot.skill_id or sections != snapshot.sections:
        fail('IDENTITY_MISMATCH', INDEX_PATH, '索引与原快照不一致。')
    by_id = {section.section_id: section for section in sections}
    if any(sid not in by_id for sid in section_ids):
        fail('UNKNOWN_SECTION', INDEX_PATH, '所选章节不在指定 Skill 索引中。')
    return {'common': document(files['SKILL.md'], snapshot.skill_id),
            'sections': {sid: files[by_id[sid].file_ref] for sid in section_ids}}
