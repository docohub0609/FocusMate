"""Discover validated package metadata; never load bodies into the registry."""
from copy import deepcopy
from pathlib import Path
import stat

from focusmate.contracts.skill_files import _linked
from focusmate.core.skill_package import validate_skill_package

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / 'skills'
FIELDS = ('skillId', 'version', 'hash', 'appliesTo', 'capabilities', 'requiredInputs',
          'requiredTools', 'coachPolicy', 'goalStateWriteAllowed', 'inputSchemaRef', 'outputSchemaRef')


class SkillRegistry:
    """A replaceable metadata snapshot, keyed by explicit (skillId, version)."""

    def __init__(self):
        self._entries = {}

    def refresh(self, roots=None):
        """Bad packages are isolated; root failures preserve the entire old snapshot."""
        roots = [DEFAULT_ROOT] if roots is None else roots
        candidates, issues, root_failed = {}, [], False
        for supplied_root in roots:
            root = Path(supplied_root).absolute()
            try:
                info = root.lstat()
                if _linked(info) or not stat.S_ISDIR(info.st_mode):
                    raise OSError('unsafe root')
                # Finish enumeration before processing any packages under this root.
                children = sorted(root.iterdir(), key=lambda path: path.name)
                children = [(child, child.lstat()) for child in children]
            except OSError:
                root_failed = True
                issues.append({'code': 'INVALID_ROOT', 'path': str(root),
                               'message': '扫描根目录不存在、不可读或是符号链接/重解析点；保留原列表。'})
                continue
            for child, info in children:
                if not _linked(info) and stat.S_ISREG(info.st_mode):
                    continue
                report = validate_skill_package(child)
                if not report['packageValid']:
                    issues.extend({**issue, 'source': str(child)} for issue in report['issues'])
                    continue
                descriptor, metadata = report['descriptor'], report['metadata']
                key = (descriptor['skillId'], descriptor['version'])
                entry = {field: deepcopy(descriptor[field]) for field in FIELDS}
                entry.update(name=metadata['name'], description=metadata['description'], sources=[str(child)])
                entry.update(contentReview=report['contentReview'], toolAvailability=report['toolAvailability'])
                candidates.setdefault(key, []).append(entry)
        snapshot = {}
        for key, copies in sorted(candidates.items()):
            sources = sorted({source for copy in copies for source in copy['sources']})
            if len({copy['hash'] for copy in copies}) != 1:
                issues.append({'code': 'IDENTITY_CONFLICT', 'path': key[0], 'version': key[1],
                               'sources': sources, 'message': '同一 Skill ID 和版本存在不同内容指纹，全部隔离。'})
                continue
            entry = copies[0]
            entry['sources'] = sources
            snapshot[key] = entry
        if not root_failed:
            self._entries = snapshot
        issues.sort(key=lambda issue: (issue.get('source', issue['path']), issue['code']))
        return {'refreshSucceeded': not root_failed, 'allValid': not issues,
                'skills': self.list(), 'issues': issues}

    def list(self, capability=None, applies_to=None):
        """Match exact labels with AND, returning independent copies in ID/version order."""
        return deepcopy([entry for _, entry in sorted(self._entries.items())
                         if (capability is None or capability in entry['capabilities'])
                         and (applies_to is None or applies_to in entry['appliesTo'])])

    def get(self, skill_id, version):
        """Return only the requested integer version, never infer a latest version."""
        if type(version) is not int:
            raise TypeError('version 必须是明确的整数版本。')
        return deepcopy(self._entries.get((skill_id, version)))
